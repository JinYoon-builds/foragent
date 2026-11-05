"""Locked acceptance: a planning turn cannot end quietly while old decisions have no issue (#1501).

Contract declared by this issue:
- `fa init --decision-hooks yes` installs a Claude Code Stop hook (rerunning keeps one copy).
  An explicit flag, because merged locks pin the init prompts and the Claude settings init
  writes without consent.
- The hook checks facts only: how many decisions in docs/decisions.md have no linked issue
  (the links `fa decisions` already derives) and how many days old the oldest is (its
  heading date). Past a limit it blocks the stop once, with both numbers; a stop that is
  already continuing because of a stop hook passes.
- `fa decisions drop Dnn --why REASON` marks a decision dropped; a dropped or linked
  decision no longer counts. A drop without a reason, or of an unknown id, is refused.
- Sessions Foragent launches (FORAGENT_ROLE set, such as lane workers) and folders outside
  a Foragent project are never blocked.
Pinned: Claude Code's Stop hook protocol (exit 2 or a block answer), the two numbers and the
commands above. The limit itself (beyond: one decision recorded today never blocks; four or
more unlinked decisions older than seven weeks always do), wording, where a drop is recorded
and the hook command are the lane's choice.
"""
from datetime import date, datetime, timedelta, timezone
from io import BytesIO, TextIOWrapper
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

import pytest
import yaml

from foragent import cli
from foragent.init import init_repo
from foragent.state import StateStore
from foragent.tracker import FakeTracker, Issue
from test_fa_init_allow import SetupGit
from test_local_truth import record_gh
from test_skills_autosync import cli_exit

REPO = "example/project"
# (id, title, age in days); D12 already has issue #50.
DECISIONS = [("D7", "Login uses email links", 61), ("D9", "Keep one gate", 50),
             ("D12", "Lanes are areas", 45), ("D20", "Reports go out on Fridays", 38),
             ("D25", "Night runs stop at six", 30), ("D31", "Docs live in one place", 21)]


def dated(days_ago):
    return (date.today() - timedelta(days=days_ago)).isoformat()


def ledger(rows):
    return "# Decisions\n\n" + "".join(
        f"### {identity}. {title} ({dated(age)}, Jin)\n- 결정: {title.lower()}.\n\n" for identity, title, age in rows)


def ages(days_ago):
    """The age in days, by the local or the UTC calendar."""
    born = date.today() - timedelta(days=days_ago)
    return {(today - born).days for today in (date.today(), datetime.now(timezone.utc).date())}


@pytest.fixture
def project(tmp_path, monkeypatch):
    home, root = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    root.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    for key in ("CLAUDE_CONFIG_DIR", "FORAGENT_ROLE", "FORAGENT_SESSION_ID", "CLAUDE_ENV_FILE"):
        monkeypatch.delenv(key, raising=False)
    for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(home / key.lower()))
    monkeypatch.setenv("FORAGENT_VIEW", "off")
    monkeypatch.setenv("FORAGENT_UPDATE_CHECK", "off")
    monkeypatch.setattr(cli, "kick_tick", lambda *args: None)
    (root / ".foragent.yml").write_text(yaml.safe_dump({
        "repo": REPO, "checks": "local", "executor": "claude", "language": "en", "skill_libraries": [],
    }), encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs/decisions.md").write_text(ledger(DECISIONS), encoding="utf-8")
    (home / ".claude").mkdir()
    (home / ".claude/settings.json").write_text("{}\n", encoding="utf-8")
    tracker = FakeTracker(login="human")
    monkeypatch.setattr(cli, "init_repo", lambda root_arg, **kwargs: init_repo(
        root, git=SetupGit(root), tracker=tracker, gh_run=lambda args, input=None: "", **kwargs))
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")  # The person also accepts if asked.
    store = StateStore.for_repo(REPO)
    store.record_issue(Issue(50, "Area lanes", '<!-- foragent:decisions ["D12"] -->', frozenset(), "open"), {})
    settings = [root / ".claude/settings.json", root / ".claude/settings.local.json", home / ".claude/settings.json"]
    argv = ["init", "--repo-root", str(root), "--no-push", "--no-launchd", "--no-board",
            "--statusline", "no", "--starter", "no", "--fa-allow", "no", "--decision-hooks", "yes"]

    class Case:
        pass

    case = Case()
    case.root, case.home, case.store, case.settings, case.patch = root, home, store, settings, monkeypatch
    case.gh = []

    def install():
        code = cli_exit(cli.main, argv)
        assert code == 0, "fa init --decision-hooks yes installs the decision stop hook"
        case.gh = record_gh(monkeypatch)

    case.install = install
    yield case
    assert case.gh == [], "The stop check reads local records only"


def _commands(settings, event):
    for path in settings:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        for group in (data.get("hooks") or {}).get(event) or []:
            yield from (hook["command"] for hook in group.get("hooks", []) if hook.get("type") == "command")


def _run(command, payload, cwd, monkeypatch):
    argv = shlex.split(command)
    while argv and "=" in argv[0]:
        argv = argv[1:]
    text = json.dumps(payload).encode("utf-8")
    if argv and Path(argv[0]).name == "fa":
        stdin = TextIOWrapper(BytesIO(text), encoding="utf-8")
        stdout, stderr = (TextIOWrapper(BytesIO(), encoding="utf-8") for _ in range(2))
        with monkeypatch.context() as patch:
            patch.setattr(sys, "stdin", stdin)
            patch.setattr(sys, "stdout", stdout)
            patch.setattr(sys, "stderr", stderr)
            patch.chdir(cwd)
            code = cli_exit(cli.main, argv[1:])
            stdout.flush()
            stderr.flush()
        return code, stdout.buffer.getvalue().decode("utf-8"), stderr.buffer.getvalue().decode("utf-8")
    done = subprocess.run(command, shell=True, input=text, capture_output=True, cwd=cwd,
                          env=dict(os.environ), timeout=60)
    return done.returncode, done.stdout.decode("utf-8"), done.stderr.decode("utf-8")


def stop(case, *, active=False, cwd=None):
    """The Stop hooks' answers: a list of block reasons (empty when the turn may end)."""
    cwd = cwd or case.root
    payload = {"session_id": "pm-session", "transcript_path": str(case.home / "pm-session.jsonl"),
               "cwd": str(cwd), "hook_event_name": "Stop", "stop_hook_active": active}
    reasons = []
    for command in _commands(case.settings, "Stop"):
        code, out, err = _run(command, payload, cwd, case.patch)
        if code == 2:
            reasons.append(err)
            continue
        assert code == 0, f"A stop hook that lets the turn end exits 0: {command} -> {code} {err!r}"
        try:
            answer = json.loads(out) if out.strip() else {}
        except ValueError:
            answer = {}
        if isinstance(answer, dict) and answer.get("decision") == "block":
            reasons.append(str(answer.get("reason") or ""))
    return reasons


def number(value, text):
    return re.search(rf"(?<![0-9]){value}(?![0-9])", text) is not None


def assert_numbers(reason, count, days_ago):
    assert number(count, reason), f"Name how many decisions have no issue ({count}): {reason!r}"
    assert any(number(age, reason) for age in ages(days_ago)), f"Name the oldest one's age in days: {reason!r}"


def link(case, number_, *identities):
    marker = "<!-- foragent:decisions " + json.dumps(list(identities)) + " -->"
    case.store.record_issue(Issue(number_, "Ordered", marker, frozenset(), "open"), {})


def drop(case, *argv):
    return cli_exit(cli.main, ["decisions", "drop", *argv, "--repo-root", str(case.root)])


def test_the_stop_hook_blocks_once_with_the_count_and_the_oldest_age(project):
    case = project
    case.install()
    reasons = stop(case)
    assert len(reasons) == 1, f"Five unlinked decisions, the oldest 61 days old, block the stop once: {reasons}"
    assert_numbers(reasons[0], 5, 61)
    assert stop(case, active=True) == [], "A stop already continuing because of a stop hook is not blocked again"
    case.gh = []
    assert cli_exit(cli.main, ["init", "--repo-root", str(case.root), "--no-push", "--no-launchd", "--no-board",
                               "--statusline", "no", "--starter", "no", "--fa-allow", "no",
                               "--decision-hooks", "yes"]) == 0
    case.gh = record_gh(case.patch)
    assert len(stop(case)) == 1, "Rerunning init keeps one copy of the hook"


def test_linking_or_dropping_with_a_reason_clears_a_decision(project):
    case = project
    case.install()
    link(case, 51, "D7")
    reasons = stop(case)
    assert len(reasons) == 1, "Four unlinked decisions, the oldest 50 days old, still block"
    assert_numbers(reasons[0], 4, 50)
    assert drop(case, "D9") != 0, "A drop needs a reason"
    assert drop(case, "D99", "--why", "Never recorded.") != 0, "Only a recorded decision can be dropped"
    assert_numbers(stop(case)[0], 4, 50)
    assert drop(case, "D9", "--why", "One gate became the merge owner.") == 0
    for reason in stop(case):
        assert_numbers(reason, 3, 38)
    link(case, 52, "D20", "D25")
    assert drop(case, "D31", "--why", "Superseded by the docs owner role.") == 0
    assert stop(case) == [], "Every decision linked or dropped: the turn ends"


def test_lane_workers_other_folders_and_a_fresh_decision_are_not_blocked(project, tmp_path):
    case = project
    case.install()
    assert len(stop(case)) == 1
    case.patch.setenv("FORAGENT_ROLE", "launched")
    assert stop(case) == [], "Sessions Foragent launches, such as lane workers, are never blocked"
    case.patch.delenv("FORAGENT_ROLE")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert stop(case, cwd=elsewhere) == [], "Outside a Foragent project the hook does nothing"
    (case.root / "docs/decisions.md").write_text(ledger([("D40", "Ship the TUI next", 0)]), encoding="utf-8")
    assert stop(case) == [], "One decision recorded today does not block the turn"
