"""Acceptance test for ux-researcher-calls (#1654). Locked by Foragent; do not edit.

The gate calls the default UX researcher role when an order finishes and once a day.
Its session sees the README and the product, not source code or internal state, and a
find reaches the orchestrator inbox only after a second, separate session confirms it.
Sessions run offline: the gate's launch is intercepted at the process boundary, its
recorded command is run in-process, and the executor is a fake that records what each
session was given.
"""

from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from foragent import capacity, executors, scheduler
from foragent.executors import ExecResult
from test_architect_project_schedule import order_children
from test_board_status_groups import offline_github  # noqa: F401
from test_orch_review_handoff import bind_cli, make_case, tick_context
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401

SKILL = "fa-ux-researcher"
SOURCE_TOKEN = "UXR-SOURCE-SENTINEL-4f1c"
README_TOKEN = "UXR-README-SENTINEL-9a2e"
MARK = "UXR-FIND-7c3d"
FIND = f"{MARK}: fa doctor before init does not suggest fa init"


def ux_role():
    """The default role that carries the UX researcher skill, from the derived defaults.

    On a build without the role, fall back to a plain entry with no call conditions, so
    the behaviour assertions below (not this lookup) say what is missing.
    """
    from foragent.config import Config
    from foragent.roles import load_roles

    for name, entry in load_roles(Config()).items():
        if SKILL in entry["skills"]:
            return name, {key: entry[key] for key in ("agent", "skills", "memory", "signals")}
    return "ux-researcher", {"agent": SKILL, "skills": [SKILL],
                             "memory": f".claude/agent-memory/{SKILL}/", "signals": []}


@pytest.fixture
def ux_case(git_history, tmp_path, monkeypatch, fake_workloads):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true", language="en")
    case.main = bind_cli(case, monkeypatch)
    case.store.finish_run(1, "merged")
    case.tracker.close_issue(1)
    case.tracker.remove_labels(1, ["running", "approved"])
    order_children(case, 900, [1])
    case.role, entry = ux_role()
    case.ctx.config = replace(case.config, roles={case.role: entry})
    # What a person sees, and what they never see: a README and the product's source.
    (case.root / "README.md").write_text(f"# Demo\n\nInstall with uv. {README_TOKEN}\n", encoding="utf-8")
    source = case.root / "src/demo/core.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(f"SECRET = {SOURCE_TOKEN!r}\n", encoding="utf-8")
    case.launches, case.pending, case.turns = [], [], []
    case.process_table = fake_workloads
    case.on_turn = lambda kwargs: "Nothing to change."

    def launch(argv, *args, **kwargs):
        argv = [str(part) for part in argv]
        if "foragent.cli" not in argv:
            return original(argv, *args, **kwargs)
        # No foragent session ever starts on the machine; settle() runs it in-process.
        child = SimpleNamespace(pid=981001 + len(case.launches), argv=argv, kwargs=kwargs, returncode=None,
                                poll=lambda: None)
        case.launches.append(child)
        case.pending.append(child)
        fake_workloads.add(child.pid, cwd=case.root, start_time=float(child.pid))
        return child

    def execute(**kwargs):
        case.turns.append(kwargs)
        message = case.on_turn(kwargs)
        return ExecResult(0, False, message, kwargs["log_path"])

    original = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(executors, "executor_for", lambda name: SimpleNamespace(run=execute))
    for executor in (executors.ClaudeExecutor, executors.CodexExecutor):
        monkeypatch.setattr(executor, "run", lambda self, **kwargs: execute(**kwargs))
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0.5))
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 0.5))
    monkeypatch.setattr(capacity, "machine_root", lambda: case.store.root.parent)
    capacity.write_state(case.store, 2, case.clock[0])
    return case


def settle(case, ticks=3):
    """Run gate ticks a minute apart; each session the gate launched runs to its end."""
    first = len(case.turns)
    for _ in range(ticks):
        assert scheduler.tick(tick_context(case)).skipped is None
        while case.pending:
            child = case.pending.pop(0)
            child.returncode = cli_exit(case.main, child.argv[3:])
            case.process_table.exit(child.pid)
        case.clock[0] += timedelta(minutes=1)
    return case.turns[first:]


def mentions(turn, order):
    return f"#{order}" in turn["prompt"]


def test_gate_calls_ux_researcher_once_per_finished_order_and_once_a_day(ux_case):
    case = ux_case
    day0 = settle(case)
    assert len([turn for turn in day0 if mentions(turn, 900)]) == 1, (
        "A finished order must call the UX researcher exactly once, for that order")
    assert len([turn for turn in day0 if not mentions(turn, 900)]) <= 1, "At most one daily pass a day"
    case.clock[0] += timedelta(hours=2)
    assert settle(case) == [], "With no new finished order the same day, nothing more is called"

    case.tracker.add_issue(3, "Second order's only child", case.tracker.get_issue(1).body, state="closed")
    order_children(case, 901, [3])
    second = settle(case)
    assert len(second) == 1 and mentions(second[0], 901) and not mentions(second[0], 900), (
        "A newly finished order calls the UX researcher once, for that order only")

    case.clock[0] += timedelta(days=1, hours=1)
    daily = settle(case)
    assert len(daily) == 1, "A new day calls the UX researcher once for the core flows"
    assert not mentions(daily[0], 900) and not mentions(daily[0], 901), (
        "The daily pass is not a repeat of an order that was already covered")
    case.clock[0] += timedelta(hours=3)
    assert settle(case) == [], "The daily pass does not repeat within the same day"


@pytest.mark.parametrize("kind", ("claude", "codex"))
def test_ux_researcher_session_gets_readme_and_product_but_not_source_or_state(ux_case, kind):
    case = ux_case
    case.ctx.config = replace(case.ctx.config, executor=kind)
    turns = settle(case)
    assert turns, "A finished order must start a UX researcher session"
    turn = turns[0]
    given = [turn["prompt"], *(str(path) for path in turn.get("add_dirs") or ())]
    assert SOURCE_TOKEN not in turn["prompt"], "The session must not be given the product's source code"
    state = str(case.store.root)
    assert not any(state in text for text in given), "The session must not be given the internal state directory"
    worktree = Path(turn["worktree"]).resolve()
    assert worktree != Path(case.root).resolve(), "The session must not run in the project checkout"
    reachable = [path for path in worktree.rglob("*") if path.is_file()]
    assert not any(SOURCE_TOKEN in path.read_text(encoding="utf-8", errors="ignore") for path in reachable), (
        "No source file may be reachable from the session's working directory")
    assert not Path(state).resolve().is_relative_to(worktree), "Internal state must not sit under its working directory"
    readme = README_TOKEN in turn["prompt"] or any(
        README_TOKEN in path.read_text(encoding="utf-8", errors="ignore") for path in reachable)
    assert readme, "The session must be given the project's README"


def inbox(case, capsys):
    capsys.readouterr()
    assert cli_exit(case.main, ["orch", "inbox", "--json", "--repo-root", str(case.root)]) == 0
    payload = json.loads(capsys.readouterr().out)
    return [row for row in payload if MARK in json.dumps(row, ensure_ascii=False)]


@pytest.mark.parametrize("confirmed", (True, False), ids=("confirmed", "not-reproduced"))
def test_find_reaches_inbox_only_after_a_second_session_confirms_it(ux_case, capsys, confirmed):
    case = ux_case
    finding = len(case.turns)

    def session(kwargs):
        # A session reports through the roles' one way out, `fa orch request --from <role>`.
        given_find = MARK in kwargs["prompt"]
        if not given_find and len(case.turns) - 1 != finding:
            return "Nothing to change."  # Only the first session finds the problem.
        if given_find and not confirmed:
            return "Could not reproduce; nothing to change."
        code = cli_exit(case.main, ["orch", "request", "--repo-root", str(case.root),
                                    "--from", case.role, "--text", FIND])
        assert code == 0, "The UX researcher must be able to report a find"
        return "Reported one find." if not given_find else "Reproduced the find."

    case.on_turn = session
    assert scheduler.tick(tick_context(case)).skipped is None
    child = case.pending.pop(0) if case.pending else None
    assert child is not None, "A finished order must start a UX researcher session"
    child.returncode = cli_exit(case.main, child.argv[3:])
    case.process_table.exit(child.pid)
    first = case.turns[finding]
    assert MARK not in first["prompt"], "The first session finds the problem on its own"
    if len(case.turns) == finding + 1:
        assert inbox(case, capsys) == [], "An unconfirmed find must not reach the orchestrator inbox"
    case.clock[0] += timedelta(minutes=1)
    settle(case)
    checks = [turn for turn in case.turns[finding + 1:] if MARK in turn["prompt"]]
    assert len(checks) == 1, "Exactly one second session must be given the find to confirm it"
    case.clock[0] += timedelta(hours=1)
    settle(case)
    rows = inbox(case, capsys)
    if confirmed:
        assert len(rows) == 1, "A confirmed find reaches the inbox once, as a proposal"
    else:
        assert rows == [], "A find the second session cannot reproduce never reaches the inbox"
