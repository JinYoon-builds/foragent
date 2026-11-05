"""Locked acceptance: a planning session starts with the decisions that have no issue yet (#1500).

The list comes from FA records (docs/decisions.md and the local issue facts that
`fa decisions` already reads), not from the session, so /clear and piling notes
cannot hide it. The person installs nothing new: the Claude hooks that
`fa init --statusline yes` already installs carry it. Pinned: Claude Code's
SessionStart hook protocol (the hook's additionalContext reaches the session),
the order (oldest first) and which decisions appear. Wording, layout and the
hook command are the lane's choice.
"""
from datetime import date, timedelta
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


def day(days_ago):
    return (date.today() - timedelta(days=days_ago)).isoformat()


# Ledger order is age order: decisions.md is append-only.
DECISIONS = [
    ("D7", "Login uses email links", 41),
    ("D12", "Lanes are areas", 30),
    ("D20", "Reports go out on Fridays", 22),
    ("D31", "Night runs stop at six", 3),
]


def ledger():
    return "# Decisions\n\n" + "".join(
        f"### {identity}. {title} ({day(age)}, Jin)\n- 결정: {title.lower()}.\n\n"
        for identity, title, age in DECISIONS)


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
    (root / "docs/decisions.md").write_text(ledger(), encoding="utf-8")
    (home / ".claude").mkdir()
    (home / ".claude/settings.json").write_text("{}\n", encoding="utf-8")
    tracker = FakeTracker(login="human")
    monkeypatch.setattr(cli, "init_repo", lambda root_arg, **kwargs: init_repo(
        root, git=SetupGit(root), tracker=tracker, gh_run=lambda args, input=None: "", **kwargs))
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    argv = ["init", "--repo-root", str(root), "--no-push", "--no-launchd", "--no-board",
            "--statusline", "yes", "--starter", "no", "--fa-allow", "no"]
    assert cli_exit(cli.main, argv) == 0, "fa init installs the existing Claude hooks"
    store = StateStore.for_repo(REPO)
    # Issue #50 carries D12: an order already exists for it.
    store.record_issue(Issue(50, "Area lanes", '<!-- foragent:decisions ["D12"] -->', frozenset(), "open"), {})
    gh = record_gh(monkeypatch)
    settings = [root / ".claude/settings.json", root / ".claude/settings.local.json", home / ".claude/settings.json"]
    yield root, store, settings, monkeypatch
    assert gh == [], "Showing decisions reads local records only"


def _matches(matcher, value):
    return not matcher or matcher == "*" or value in matcher.split("|") or re.fullmatch(matcher, value or "")


def _commands(settings, event, value):
    for path in settings:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        for group in (data.get("hooks") or {}).get(event) or []:
            if _matches(group.get("matcher", ""), value):
                yield from (hook["command"] for hook in group.get("hooks", []) if hook.get("type") == "command")


def _run(command, payload, root, monkeypatch):
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
            patch.chdir(root)
            code = cli_exit(cli.main, argv[1:])
            stdout.flush()
        return code, stdout.buffer.getvalue().decode("utf-8")
    done = subprocess.run(command, shell=True, input=text, capture_output=True, cwd=root,
                          env=dict(os.environ), timeout=60)
    return done.returncode, done.stdout.decode("utf-8")


def session_context(project, source, session="pm-session"):
    """What the SessionStart hooks add to the session's context (Claude Code's hook protocol)."""
    root, _store, settings, monkeypatch = project
    payload = {"hook_event_name": "SessionStart", "source": source, "session_id": session,
               "cwd": str(root), "transcript_path": str(root / f"{session}.jsonl")}
    parts = []
    for command in _commands(settings, "SessionStart", source):
        code, out = _run(command, payload, root, monkeypatch)
        assert code == 0, f"A SessionStart hook never stops the session: {command} -> {code}"
        if not out.strip():
            continue
        try:
            answer = json.loads(out)
        except ValueError:
            parts.append(out)  # Plain SessionStart stdout also reaches the session.
            continue
        context = (answer.get("hookSpecificOutput") or {}).get("additionalContext")
        if isinstance(context, str):
            parts.append(context)
    return "\n".join(parts)


def shown(context):
    """The ledger decisions the context names, in the order it names them."""
    titles = {title: identity for identity, title, _age in DECISIONS}
    found = sorted((context.find(title), identity) for title, identity in titles.items() if title in context)
    return [identity for _at, identity in found]


@pytest.mark.parametrize("source", ["startup", "resume"])
def test_a_planning_session_starts_with_unordered_decisions_oldest_first(project, source):
    context = session_context(project, source)
    assert shown(context) == ["D7", "D20", "D31"], (
        f"Decisions with no issue, oldest first; D12 already has #50: {context!r}")
    for identity, title, _age in DECISIONS:
        if identity != "D12":
            assert context.count(title) == 1, f"{identity} is listed once"
            assert re.search(rf"(?<![A-Za-z0-9_]){identity}(?![A-Za-z0-9_])", context), (
                f"Name {identity} so the PM can cite it")


def test_the_list_follows_the_records_not_the_session(project):
    root, store, _settings, monkeypatch = project
    assert shown(session_context(project, "startup")) == ["D7", "D20", "D31"]
    for n in range(40):
        assert cli_exit(cli.main, ["order", "note", "--defer", "later", "--kind", "idea",
                                   "--text", f"Idea number {n}", "--repo-root", str(root)]) == 0
    assert shown(session_context(project, "resume")) == ["D7", "D20", "D31"], (
        "Piling notes do not push decisions out")
    store.record_issue(Issue(51, "Friday reports", '<!-- foragent:decisions ["D20"] -->', frozenset(), "open"), {})
    assert shown(session_context(project, "startup", "pm-after-clear")) == ["D7", "D31"], (
        "A decision linked to an issue since the last start is no longer shown")
    store.record_issue(Issue(52, "Login and nights", '<!-- foragent:decisions ["D7", "D31"] -->',
                             frozenset(), "open"), {})
    context = session_context(project, "startup", "pm-third")
    assert shown(context) == [] and not re.search(r"(?<![A-Za-z0-9_])D[1-9][0-9]*(?![A-Za-z0-9_])", context), (
        "With every decision ordered, no decision list is added")


def test_launched_sessions_do_not_get_the_list(project):
    root, _store, _settings, monkeypatch = project
    assert shown(session_context(project, "startup")) == ["D7", "D20", "D31"]
    monkeypatch.setenv("FORAGENT_ROLE", "launched")
    assert shown(session_context(project, "startup", "lane-worker")) == [], (
        "Only a planning session gets the list; sessions Foragent launches do not")
