"""Locked: person-only commands refuse inside sessions Foragent launched.

A session's identity is whatever environment Foragent gives the process it launches.
These tests launch lane and role sessions through Foragent's own launch paths with an
offline stand-in for the model CLI, record the environment that process received, and
run the command inside that environment. No variable name or value is prescribed for
lane or role sessions; the orchestrator's existing FORAGENT_ROLE contract is reused.
"""

from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from foragent import capacity, cli, executors
from test_approval_record import waiting_pr
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401

@pytest.fixture(autouse=True)
def calm_machine(monkeypatch):
    """A loaded host must not hold these offline commands at admission; admission is not the subject."""
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 0.5))
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")


PERSON_ONLY = {
    "orch resolve": ["orch", "resolve", "1", "--verdict", "pass", "--note", "Checked the work"],
    "orch merge": ["orch", "merge", "2", "--approved-by", "human"],
    "skills add --approve": ["skills", "add", "https://github.com/offline/pack", "--approve",
                             "--role", "demo=tool"],
    "settings set": ["settings", "set", "review", "careful"],
    "init --fa-allow": ["init", "--fa-allow", "yes", "--no-push", "--no-launchd", "--no-board",
                        "--statusline", "no", "--starter", "no"],
    "role add": ["role", "add", "auditor", "--scope", "tests/**", "--why", "Checked"],
    "role set": ["role", "set", "architect", "--idle-days", "3", "--why", "Checked"],
    "role remove": ["role", "remove", "tester"],
    "lanes skills": ["lanes", "skills", "view", "principle-fix-root-causes"],
}

STAND_IN = """#!{python}
import json, os, sys
from pathlib import Path
sys.stdin.read()
with open({record!r}, "a", encoding="utf-8") as stream:
    stream.write(json.dumps({{"argv": sys.argv, "env": dict(os.environ)}}) + "\\n")
args = sys.argv[1:]
if "-o" in args:
    Path(args[args.index("-o") + 1]).write_text("done", encoding="utf-8")
    print(json.dumps({{"type": "thread.started", "thread_id": "offline"}}))
else:
    print(json.dumps({{"type": "system", "subtype": "init", "session_id": "offline"}}))
    print(json.dumps({{"type": "result", "result": "done", "num_turns": 1}}))
"""


def stand_in_models(tmp_path, monkeypatch):
    """Offline `claude` and `codex` on PATH that record the environment they were started with."""
    directory = tmp_path / "model-bin"
    directory.mkdir(exist_ok=True)
    record = directory / "sessions.jsonl"
    for name in ("claude", "codex"):
        path = directory / name
        path.write_text(STAND_IN.format(python=sys.executable, record=str(record)), encoding="utf-8")
        path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")
    return record


def last_session(record):
    lines = record.read_text(encoding="utf-8").splitlines()
    assert lines, "Foragent did not start a session"
    return json.loads(lines[-1])["env"]


def project(root, executor="claude", roles=None):
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(exist_ok=True)
    (root / ".git/config").write_text('[remote "origin"]\nurl = git@github.com:offline/person-only.git\n')
    values = {"repo": "offline/person-only", "language": "en", "approvers": ["human"],
              "executor": executor, "checks": "local", "skill_libraries": [],
              "lanes": {"view": {"paths": ["view/**"], "agent": "fa-view"}}}
    if roles:
        values["roles"] = {name: {"agent": f"fa-{name}", "skills": [], "signals": [],
                                  "memory": f".claude/agent-memory/fa-{name}/"} for name in roles}
    (root / ".claude/agents").mkdir(parents=True, exist_ok=True)
    (root / ".claude/agents/fa-view.md").write_text("---\nname: fa-view\ndescription: View lane\n---\nBody\n")
    (root / ".foragent.yml").write_text(yaml.safe_dump(values), encoding="utf-8")
    return root


def started_by(monkeypatch, launcher):
    for name in ("FORAGENT_ROLE", "FORAGENT_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    if launcher == "orchestrator":
        monkeypatch.setenv("FORAGENT_ROLE", "orchestrator")
        monkeypatch.setenv("FORAGENT_SESSION_ID", "orchestrator-session")


def role_session(tmp_path, monkeypatch, role, launcher, executor="claude"):
    """Environment of a role session started the way `fa orch architect|role` starts it."""
    record = stand_in_models(tmp_path, monkeypatch)
    root = project(tmp_path / f"role-{role}", executor)
    with monkeypatch.context() as scoped:
        scoped.chdir(root)
        started_by(scoped, launcher)
        code = cli.main(["gate", "run-architect", "--repo-root", str(root),
                         "--started-at", "2026-11-03T00:00:00+00:00", "--why", "Evidence", "--role", role])
    assert code == 0
    return last_session(record)


def lane_session(tmp_path, monkeypatch, executor, launcher="gate"):
    """Environment of a lane run's session, started through the configured executor."""
    record = stand_in_models(tmp_path, monkeypatch)
    worktree = tmp_path / f"lane-{executor}"
    worktree.mkdir()
    agent = {"agent": "fa-view"} if executor == "claude" else {}
    with monkeypatch.context() as scoped:
        started_by(scoped, launcher)
        result = executors.executor_for(executor).run(
            worktree=worktree, prompt="Implement the issue.", model=None, timeout_s=60,
            log_path=tmp_path / f"lane-{executor}.log", **agent)
    assert result.exit_code == 0
    return last_session(record)


def enter(monkeypatch, environment):
    """Run the rest of the test inside ``environment`` (what the launched session saw)."""
    for name in set(os.environ) - set(environment):
        if name != "PYTEST_CURRENT_TEST":
            monkeypatch.delenv(name)
    for name, value in environment.items():
        if name != "PYTEST_CURRENT_TEST" and os.environ.get(name) != value:
            monkeypatch.setenv(name, value)


def files(root):
    return {str(path): path.read_bytes() for path in sorted(Path(root).rglob("*")) if path.is_file()}


def refused_in_one_line(capsys, code):
    output = capsys.readouterr()
    lines = [line for line in output.err.splitlines() if line.strip()]
    assert code != 0, f"A person-only command ran inside a launched session: {output.out}{output.err}"
    assert len(lines) == 1 and not output.out.strip(), (
        f"The refusal must be one line saying why: {output.out!r} {output.err!r}")
    return lines[0]


SESSIONS = {
    "lane run (claude)": lambda tmp_path, monkeypatch: lane_session(tmp_path, monkeypatch, "claude"),
    "lane run (codex)": lambda tmp_path, monkeypatch: lane_session(tmp_path, monkeypatch, "codex"),
    "lane run kicked from the orchestrator":
        lambda tmp_path, monkeypatch: lane_session(tmp_path, monkeypatch, "claude", "orchestrator"),
    "architect started by the orchestrator":
        lambda tmp_path, monkeypatch: role_session(tmp_path, monkeypatch, "architect", "orchestrator"),
    "docs started by the gate (codex)":
        lambda tmp_path, monkeypatch: role_session(tmp_path, monkeypatch, "docs", "gate", "codex"),
}


@pytest.mark.parametrize("launcher", ["orchestrator", "gate"])
def test_architect_session_cannot_record_a_verdict_but_a_person_can(
    git_history, tmp_path, monkeypatch, capsys, launcher,
):
    person = dict(os.environ)
    session = role_session(tmp_path, monkeypatch, "architect", launcher)
    case, main, pr, head = waiting_pr(git_history, tmp_path, monkeypatch, "needs-human")
    labels = set(case.tracker.get_issue(1).labels)
    comments = list(case.tracker.list_comments(1))
    state = files(case.store.root)
    capsys.readouterr()

    with monkeypatch.context() as scoped:
        enter(scoped, session)
        code = cli_exit(main, ["orch", "resolve", "1", "--verdict", "pass", "--note", "Looks fine"])
    refused_in_one_line(capsys, code)
    assert case.tracker.merged_prs == [] and case.tracker.get_issue(pr).state == "open"
    assert case.tracker.get_issue(1).state == "open" and set(case.tracker.get_issue(1).labels) == labels
    assert case.tracker.list_comments(1) == comments, "A refused verdict leaves no trace on GitHub"
    assert files(case.store.root) == state, "A refused verdict changes no Foragent state"

    # The same command from the person's own terminal works as it does today.
    with monkeypatch.context() as scoped:
        enter(scoped, {**person, "PATH": os.environ["PATH"]})
        case.clock[0] += timedelta(minutes=1)
        assert main(["orch", "resolve", "1", "--verdict", "pass", "--note", "Looks fine"]) == 0
    assert case.tracker.merged_prs == [pr] and case.tracker.get_issue(1).state == "closed"


@pytest.mark.parametrize("session", list(SESSIONS))
@pytest.mark.parametrize("command", list(PERSON_ONLY))
def test_each_person_only_command_is_refused_inside_launched_sessions(
    tmp_path, monkeypatch, capsys, session, command,
):
    environment = SESSIONS[session](tmp_path, monkeypatch)
    # Every command below is valid here: from a person's terminal each one reaches GitHub,
    # starts a process or writes a file.
    root = project(tmp_path / "work", roles=["architect", "docs", "tester"])
    github, launched = [], []
    monkeypatch.setattr(cli, "GhTracker", lambda repo: github.append(repo) or pytest.fail("GitHub contacted"))
    popen = subprocess.Popen

    def watch(args, *positional, **kwargs):
        launched.append(args)
        return popen(args, *positional, **kwargs)

    before = files(tmp_path)
    capsys.readouterr()
    with monkeypatch.context() as scoped:
        enter(scoped, environment)
        scoped.chdir(root)
        scoped.setattr(subprocess, "Popen", watch)
        code = cli_exit(cli.main, PERSON_ONLY[command])
    refused_in_one_line(capsys, code)
    assert github == [] and launched == [], f"{command} reached GitHub or a process before refusing"
    assert files(tmp_path) == before, f"{command} changed files before refusing"


@pytest.mark.parametrize("seat", ["terminal", "PM session"])
def test_person_terminal_and_pm_session_keep_person_only_commands(git_history, tmp_path, monkeypatch, capsys, seat):
    for name in ("FORAGENT_ROLE", "FORAGENT_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    if seat == "PM session":
        monkeypatch.setenv("FORAGENT_SESSION_ID", "pm-session")
    root = project(tmp_path / "work")
    with monkeypatch.context() as scoped:
        scoped.chdir(root)
        assert cli.main(["settings", "set", "review", "careful"]) == 0
    assert yaml.safe_load((root / ".foragent.yml").read_text())["review"] == "careful"

    case, main, pr, head = waiting_pr(git_history, tmp_path, monkeypatch, "needs-human")
    assert main(["orch", "resolve", "1", "--verdict", "pass", "--note", "Looks fine"]) == 0
    assert case.tracker.merged_prs == [pr]


def test_orchestrator_keeps_the_verbs_its_guide_gives_it(git_history, tmp_path, monkeypatch):
    """The orchestrator records verdicts and merges approved PRs (skills/fa-orchestrator)."""
    monkeypatch.setenv("FORAGENT_ROLE", "orchestrator")
    monkeypatch.setenv("FORAGENT_SESSION_ID", "orchestrator-session")
    case, main, pr, head = waiting_pr(git_history, tmp_path, monkeypatch, "needs-human")
    assert main(["orch", "resolve", "1", "--verdict", "pass", "--note", "Looks fine"]) == 0
    assert case.tracker.merged_prs == [pr]
