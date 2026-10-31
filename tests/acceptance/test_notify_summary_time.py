"""Locked acceptance: notify-summary-time. Planning owns this contract.

The reported regression is in a lane-owned unit test, so run that test under
controlled ambient times as well as checking the public CLI's documented clock.
No production notification helper is imported or replaced.
"""

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

import pytest

from foragent import cli
from foragent.state import StateStore
from foragent.tracker import FakeTracker


ROOT = Path(__file__).resolve().parents[2]
if ROOT.name == "order-out":  # Staged here; publication puts this under tests/acceptance/.
    ROOT = ROOT.parent
TARGET = "tests/test_cli.py::test_inspect_cli_notify_logs_failure_and_retries_then_dedupes"
ITEM = "#1 Issue 1 (escalated: Choose a format)"

CHILD = r'''
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
import json
import sys
import time

import pytest

time.tzset()
wall = datetime.fromisoformat(sys.argv[1])

class AmbientClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return wall.astimezone(tz) if tz else wall.astimezone().replace(tzinfo=None)

class Probe:
    def __init__(self):
        self.outputs = []
        self.passed = 0

    def pytest_collection_modifyitems(self, items):
        assert len(items) == 1, "The original regression test must remain collected"
        # Only its ambient datetime changes. Its app fixture still fixes CLI NOW.
        items[0].module.datetime = AmbientClock

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item):
        from foragent import cli
        original = cli.main

        def observe(args=None):
            output = StringIO()
            with redirect_stdout(output):
                result = original(args)
            text = output.getvalue()
            sys.stdout.write(text)
            assert args[:2] == ["inspect", "notify"], args
            self.outputs.append((result, text))
            return result

        cli.main = observe
        try:
            yield
        finally:
            cli.main = original

    def pytest_runtest_logreport(self, report):
        if report.when == "call" and report.passed:
            self.passed += 1

probe = Probe()
result = pytest.main([sys.argv[2], "-q", "-p", "no:cacheprovider",
                      "--basetemp", sys.argv[3]], plugins=[probe])
print("Observed public CLI calls:", json.dumps(probe.outputs))
if result == 0:
    item = "#1 Issue 1 (escalated: Choose a format)\n"
    assert probe.passed == 1, "Skipping or xfail does not fix the regression"
    assert probe.outputs == [(0, item), (0, item), (0, "")], probe.outputs
raise SystemExit(result)
'''


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(home / name.lower()))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    return tmp_path


@pytest.mark.parametrize("zone", ["UTC", "Asia/Seoul"])
@pytest.mark.parametrize("wall", ["2026-10-31T07:57:00+00:00", "2026-10-31T08:06:00+00:00"])
def test_original_retry_regression_is_independent_of_ambient_hour(isolated, zone, wall):
    """07:57 was green; 08:06 is red today (08 UTC / 17 Seoul local)."""
    env = {**os.environ, "TZ": zone, "PYTHONPATH": str(ROOT / "src"),
           "TMPDIR": str(isolated), "PYTEST_ADDOPTS": ""}
    result = subprocess.run(
        [sys.executable, "-B", "-c", CHILD, wall, TARGET, str(isolated / "nested")],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, (
        f"The original public-CLI regression must pass with ambient {wall} in {zone}.\n"
        f"{result.stdout}\n{result.stderr}"
    )


@pytest.mark.parametrize("zone,summary_hour", [("UTC", 8), ("Asia/Seoul", 10), ("Asia/Seoul", 0)])
def test_cli_keeps_documented_local_summary_and_retry_contract(
    isolated, monkeypatch, capsys, caplog, zone, summary_hour,
):
    """Off-hour retries stay item-only; summaries retry and persist once per local day."""
    previous_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", zone)
    time.tzset()
    local = ZoneInfo(zone)
    due = datetime(2026, 10, 31, summary_hour, tzinfo=local)
    # For a custom hour of 10, also prove the default hour of 8 is replaced.
    now = [due - timedelta(hours=2 if summary_hour == 10 else 1)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now[0].astimezone(tz) if tz else now[0].replace(tzinfo=None)

    tracker = FakeTracker(now=lambda: now[0].astimezone(UTC), strict=True)
    tracker.add_issue(1, "Issue 1", labels=["needs-human"])
    tracker.comment(1, "<!-- foragent:needs-human reason=escalated -->\nChoose a format")
    config = "repo: owner/repo\nlanguage: en\nnotify: command:/offline/notifier\n"
    if summary_hour != 8:  # Exercise the documented default as well as overrides.
        config += f"notify_summary_hour: {summary_hour}\n"
    (isolated / ".foragent.yml").write_text(config, encoding="utf-8")
    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(cli, "GhTracker", lambda repo: tracker)
    monkeypatch.setattr(cli.StateStore, "for_repo", lambda repo: StateStore(
        isolated / "state", now=lambda: now[0].astimezone(UTC),
    ))  # Reopen persisted state for every CLI invocation.
    attempts = []
    fail = [True]

    def transport(args, **kwargs):
        assert args == ["/offline/notifier"]
        env = kwargs["env"]
        attempts.append((env["FA_TITLE"], env["FA_LINE"], env["FA_LINK"], not fail[0]))
        if fail[0]:
            raise FileNotFoundError("notifier unavailable")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", transport)

    def invoke(expected):
        before = len(attempts)
        assert cli.main(["inspect", "notify", "--repo-root", str(isolated)]) == 0
        assert capsys.readouterr().out == (expected + "\n" if expected else "")
        assert len(attempts) - before == int(bool(expected))

    try:
        invoke(ITEM)  # Failed delivery is visible and remains retryable.
        assert "notifier unavailable" in caplog.text
        fail[0] = False
        invoke(ITEM)
        invoke("")
        now[0] = due
        fail[0] = True
        invoke("Decision 1")
        fail[0] = False
        invoke("Decision 1")
        now[0] = due + timedelta(minutes=59)
        invoke("")
        now[0] = due + timedelta(hours=1)
        invoke("")
        now[0] = due + timedelta(days=1)
        invoke("Decision 1")
        invoke("")
        item = ("Foragent owner/repo", ITEM, "https://github.com/owner/repo/issues/1")
        summary = ("Foragent owner/repo · Morning summary", "Decision 1",
                   "https://github.com/owner/repo/issues")
        assert attempts == [(*item, False), (*item, True), (*summary, False),
                            (*summary, True), (*summary, True)]
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        time.tzset()
