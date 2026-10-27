"""Acceptance test for presence-statusline. Locked by Foragent; do not edit."""

from datetime import UTC, datetime, timedelta
from io import BytesIO
import json
import re
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
import yaml

from test_local_truth import record_gh
from test_skills_autosync import cli_exit, isolated_home


NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)
USER_COMMAND = "offline-user-statusline"
USER_OUTPUT = b"\x1b[32mClaude\x1b[0m\ncontext 42%"
RAW = b'{"rate_limits":{"five_hour":{"used_percentage":23.5}}}\n'


@pytest.fixture
def presence_case(tmp_path, monkeypatch, isolated_home):
    from foragent import cli, state, statusline
    from foragent.orch_inbox import ask
    from foragent.state import StateStore
    from foragent.tracker import Issue

    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    values = {"repo": "offline/presence", "language": "ko", "skill_libraries": [],
              "lanes": {slug: {"paths": [f"{slug}/**"]}
                        for slug in ("api", "web", "docs", "decision", "idle")}}
    config_path = root / ".foragent.yml"

    def configure(**changes):
        values.update(changes)
        config_path.write_text(yaml.safe_dump(values), encoding="utf-8")

    configure()
    clock = [NOW]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    for module in (cli, state, statusline):
        monkeypatch.setattr(module, "datetime", Clock)
    store = StateStore.for_repo(values["repo"], now=lambda: clock[0])
    issues = [Issue(n, f"Working {slug}", "", frozenset({"running", f"lane:{slug}"}), "open")
              for n, slug in enumerate(("api", "web", "docs"), 1)]
    issues.append(Issue(40, "Choose storage region", "",
                        frozenset({"needs-human", "lane:decision"}), "open"))
    store.refresh_local_issues(issues, [], values["lanes"], NOW)
    for n, slug in enumerate(("api", "web", "docs"), 1):
        store.start_run(n, branch=f"foragent/issue-{n}", worktree=store.worktree_path(n), lane=slug)
    # Four runs in three lanes: count working lanes, rather than worker records.
    store.start_run(4, branch="foragent/issue-4", worktree=store.worktree_path(4), lane="api")
    # One person's turn, present in both the issue snapshot and the question box.
    ask(store, "Choose storage region", ["Seoul", "Tokyo"], 1, issues=[40])
    # Today's two merges, yesterday's merge, and a failed run are distinct facts.
    for n, at, outcome in ((101, NOW - timedelta(hours=2), "merged"),
                           (102, NOW - timedelta(hours=1), "merged"),
                           (103, NOW - timedelta(hours=15), "merged"),
                           (104, NOW - timedelta(minutes=30), "checks-failed")):
        clock[0] = at - timedelta(minutes=10)
        store.start_run(n, branch=f"foragent/issue-{n}", worktree=store.worktree_path(n), lane="api")
        clock[0] = at
        if outcome == "merged":
            store.record_merge(n, at)
        store.finish_run(n, outcome=outcome)
    clock[0] = NOW
    monkeypatch.chdir(root)
    monkeypatch.delenv(statusline.NESTED_ENV, raising=False)
    record = statusline.record_path()
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"enabled": True, "previous_command": USER_COMMAND}), encoding="utf-8")
    user_calls = []
    user_output = [USER_OUTPUT]

    def forbidden(*args, **kwargs):
        pytest.fail("Presence must use local state only: no network, child process, or sleep")

    def fake_user(command, *args, **kwargs):
        assert command == USER_COMMAND, f"Unexpected external command: {command!r}"
        assert kwargs.get("input") == RAW, "Forward the user's original statusline input"
        user_calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=user_output[0], stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_user)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(time, "sleep", forbidden)
    gh_calls = record_gh(monkeypatch)

    def run():
        output = BytesIO()
        # Exercise the public CLI and its command wrapper, including binary I/O.
        with monkeypatch.context() as streams:
            streams.setattr(sys, "stdin", SimpleNamespace(buffer=BytesIO(RAW)))
            streams.setattr(sys, "stdout", SimpleNamespace(buffer=output))
            code = cli_exit(cli.main, ["statusline"])
        assert code == 0, "fa statusline must keep the user's successful exit code"
        assert gh_calls == [], "Presence must make zero GitHub calls"
        return output.getvalue()

    yield SimpleNamespace(root=root, store=store, configure=configure, run=run,
                          user_calls=user_calls, user_output=user_output, gh_calls=gh_calls)
    assert gh_calls == [], "Presence must make zero GitHub calls, including failed runs"


def assert_presence(output, *, language="ko", original=USER_OUTPUT):
    """Check the seeded facts and byte prefix; separator punctuation is free."""
    prefix = original.rstrip(b"\r\n")
    assert output.startswith(prefix), "Keep the user's statusline bytes unchanged before the presence segment"
    suffix = output[len(prefix):]
    assert "🐿".encode() in suffix, "A configured project must append the local Foragent presence segment"
    segment = suffix.decode("utf-8").rstrip("\r\n")
    assert "\n" not in segment and "\r" not in segment, "Append presence on the user's last line"
    assert len(segment) < 120, "Presence must be one short segment"
    parts = ((r"레인\s+3\s+일하는 중", r"✋\s*1(?!\d)", r"오늘 머지\s+2(?!\d)")
             if language == "ko" else
             (r"3\s+lanes working", r"✋\s*1(?!\d)", r"2\s+merged today"))
    for part in parts:
        assert re.search(part, segment), f"Presence must show the seeded local count: {part}"
    assert output.endswith(b"\n") == original.endswith(b"\n"), "Preserve the user's final-newline convention"


@pytest.mark.parametrize("language", ["ko", "en"])
@pytest.mark.parametrize("ending", [b"", b"\n"], ids=["no-final-newline", "final-newline"])
def test_presence_statusline_appends_local_counts_on_the_last_line(presence_case, language, ending):
    case = presence_case
    case.configure(language=language)
    case.user_output[0] = USER_OUTPUT + ending
    assert_presence(case.run(), language=language, original=case.user_output[0])
    assert case.user_calls == [USER_COMMAND], "Run the user's command exactly once"


def test_presence_statusline_outside_a_project_is_exactly_the_users_output(
    presence_case, tmp_path, monkeypatch,
):
    case = presence_case
    # An enabled control prevents an implementation that never adds presence
    # from passing the outside-project contract by accident.
    assert_presence(case.run())
    outside = tmp_path / "empty-folder"
    outside.mkdir()
    monkeypatch.chdir(outside)
    assert case.run() == USER_OUTPUT, "Outside a Foragent project, stdout must be byte-identical"
    assert case.user_calls == [USER_COMMAND, USER_COMMAND]


def test_presence_statusline_twenty_refreshes_are_local_and_under_two_seconds(presence_case):
    case = presence_case
    started = time.perf_counter()
    outputs = [case.run() for _ in range(20)]
    elapsed = time.perf_counter() - started
    assert elapsed < 2, f"Twenty local presence refreshes must take under 2 seconds; took {elapsed:.3f}s"
    assert case.gh_calls == [], "Twenty refreshes must make zero GitHub calls"
    assert len(case.user_calls) == 20, "Each refresh must run the user's statusline once"
    for output in outputs:
        assert_presence(output)


def test_presence_statusline_can_be_switched_off_in_project_config(presence_case):
    case = presence_case
    assert_presence(case.run())
    case.configure(presence={"statusline": False})
    assert case.run() == USER_OUTPUT, "presence.statusline: false must restore the exact user's output"
    assert case.user_calls == [USER_COMMAND, USER_COMMAND]
