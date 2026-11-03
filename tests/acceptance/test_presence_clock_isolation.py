"""Locked order test: the presence tests own their clock; the hook keeps real recency."""

import io
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest


ROOT = next(p for p in Path(__file__).resolve().parents if (p / ".foragent.yml").is_file())
CHECKS = (
    "test_a_lone_surrogate_in_one_field_replaces_only_that_character[notice-summary]",
    "test_a_lone_surrogate_in_one_field_replaces_only_that_character[notice-channel]",
    "test_a_line_separator_inside_a_field_keeps_the_notice_whole",
    "test_a_lone_surrogate_in_a_raw_notice_line_reaches_the_turn_replaced",
)


def _check_real_hook_window(tmp_path):
    """Reject disabling/widening recency or dropping/duplicating the Unicode notice."""
    from foragent import orch_inbox, presence, statusline
    from foragent.state import StateStore

    now = datetime(2042, 3, 17, 12, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    with pytest.MonkeyPatch.context() as patch:
        home = tmp_path / "hook-home"
        home.mkdir()
        patch.setenv("HOME", str(home))
        patch.setenv("XDG_DATA_HOME", str(home / "data"))
        patch.delenv("FORAGENT_ROLE", raising=False)
        patch.setattr(statusline, "datetime", Clock)
        root = tmp_path / "project"
        (root / ".git").mkdir(parents=True)
        (root / ".foragent.yml").write_text("repo: offline/clock\n", encoding="utf-8")
        (root / ".git/config").write_text(
            '[remote "origin"]\n url = https://github.com/offline/clock.git\n', encoding="utf-8",
        )
        store = StateStore.for_repo("offline/clock", now=lambda: now)
        assert presence.bind_checkout(root, now)
        orch_inbox.ask(store, "Visible question", ["a", "b"], 1)
        events = [
            {"at": (now - timedelta(hours=24)).isoformat(), "kind": "notice",
             "status": "sent", "summary": "expired-at-boundary"},
            {"at": (now - timedelta(hours=24) + timedelta(seconds=1)).isoformat(),
             "kind": "notice", "status": "sent", "summary": "a\ud800b", "channel": "push\u2028line"},
        ]
        journal = store.root / "orch-events.jsonl"
        with journal.open("a", encoding="utf-8") as stream:
            for event in events:
                stream.write(json.dumps(event) + "\n")
        before = journal.read_bytes()
        output, errors = io.BytesIO(), io.StringIO()
        patch.setattr(sys, "stderr", errors)
        assert statusline.prompt_context(
            io.BytesIO(json.dumps({"cwd": str(root)}).encode()), output,
        ) == 0
        context = json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]
        assert context.count('"a?b"') == 1 and context.count('"push\u2028line"') == 1
        assert "expired-at-boundary" not in context and "Visible question" in context
        assert errors.getvalue() == "" and journal.read_bytes() == before


def test_presence_notice_unit_checks_are_independent_of_the_host_clock(tmp_path):
    _check_real_hook_window(tmp_path)
    # These are external host clock values, not the clock a correctly isolated
    # unit test supplies. A missing, skipped, or xfailed original test is no pass.
    failures = []
    for index, wall in enumerate(("2026-10-30T12:00:00+00:00", "2026-11-01T12:00:00+00:00",
                                "2042-03-17T12:00:00+00:00")):
        report = tmp_path / f"clock-{index}.xml"
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(ROOT / "tests")))
        env["PYTEST_ADDOPTS"] = "-p no:cacheprovider"
        script = """
import sys
from datetime import datetime
import pytest
from foragent import statusline
wall = datetime.fromisoformat(sys.argv[1])
class HostClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return wall.astimezone(tz)
statusline.datetime = HostClock
raise SystemExit(pytest.main(sys.argv[2:]))
"""
        result = subprocess.run(
            [sys.executable, "-c", script, wall, "-q", "--tb=short", f"--junitxml={report}",
             *(f"tests/test_presence.py::{name}" for name in CHECKS)],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
        )
        cases = list(ET.parse(report).iter("testcase")) if report.exists() else []
        passed = (len(cases) == len(CHECKS) and {case.get("name") for case in cases} == set(CHECKS)
                  and all(not list(case) for case in cases))
        if result.returncode != 0 or not passed:
            failures.append(f"host clock {wall}:\n{result.stdout}\n{result.stderr}")
    assert not failures, "Presence unit tests leaked the host clock:\n" + "\n".join(failures)
