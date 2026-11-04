"""Acceptance test for lane test drafts. Locked by Foragent; do not edit.

A decided issue whose Done when is still the placeholder gets one drafting session by
its own lane's agent. The session writes draft tests and a plan, the draft lands outside
the source tree, lanes draft at the same time, and drafting never holds back a lane's
implementation work.

Public seams only: the gate tick with the launcher `fa gate tick` uses, the worker
command that launcher would run (captured, then run in this process), a fake executor
and a fake GitHub. Where the draft is kept, what the worker command is called and how
sessions are recorded are the lane's choice.
"""

from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from conftest import git_command
from test_orch_review_handoff import bind_cli, make_case, tick_context
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401

PLACEHOLDER = "false  # placeholder: acceptance tests are written before this issue can be approved"
GOALS = {
    11: "Show the queue length for the orch lane in one plain sentence.",
    12: "Keep the orch lane's retry note readable after a restart.",
    14: "Explain closed work that never needs a draft.",
    21: "Show the view lane's waiting reason beside its squirrel.",
}


def body(goal, done_when):
    return (
        "<!-- foragent:v1 -->\n## Goal\n" + goal + "\n\n## Out of scope\nOther work.\n\n"
        "## Done when\n```sh\n" + done_when + "\n```\n"
        "- [ ] Write acceptance tests and show the order card before approval\n"
    )


def marker(number, part):
    return f"lane-draft-{number}-{part}-c41e7"


@pytest.fixture
def drafts(git_history, tmp_path, monkeypatch, isolated_home):
    """A project with orch and view lanes, a recording launcher and a fake lane agent."""
    from foragent import capacity, executors, scheduler
    from foragent.executors import ExecResult
    from foragent.launcher import spawn_runner

    def build(**config_values):
        git_history.update({
            ".claude/agents/fa-orch.md": "---\nname: fa-orch\n---\nThe orch lane agent.\n",
            ".claude/agents/fa-view.md": "---\nname: fa-view\n---\nThe view lane agent.\n",
            "src/app.py": "VALUE = 1\n",
        }, message="Lane agents")
        git_command(git_history.clone, "fetch", "--quiet", "origin")
        git_command(git_history.clone, "merge", "--quiet", "--ff-only", "origin/main")
        case = make_case(
            git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true",
            lanes={"orch": {"paths": ["src/orch/**"], "agent": "fa-orch"},
                   "view": {"paths": ["src/view/**"], "agent": "fa-view"}},
            **config_values,
        )
        # The shared fixture starts issue #1; these scenarios begin with no run.
        case.store.finish_run(1, outcome="not-started")
        case.tracker.remove_labels(1, ["running", "approved"])
        case.tracker.close_issue(1)
        case.ready_body = case.tracker.get_issue(1).body
        case.tmp, case.main = tmp_path, bind_cli(case, monkeypatch)
        calm = ("normal", 0.5)  # A loaded host must not decide these starts.
        monkeypatch.setattr(scheduler, "machine_load", lambda: calm)
        monkeypatch.setattr(capacity, "machine_load", lambda: calm)
        monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")

        # The launcher `fa gate tick` uses, with its worker processes captured instead of spawned.
        case.launched = []
        popen = subprocess.Popen

        def capture(args, *positional, **kwargs):
            if isinstance(args, (list, tuple)) and "foragent.cli" in args:
                case.launched.append([str(part) for part in args[list(args).index("foragent.cli") + 1:]])
                return SimpleNamespace(pid=os.getpid())  # A live process; the gate may never signal it.
            return popen(args, *positional, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", capture)
        case.tick_ctx = tick_context(case)
        case.tick_ctx.launcher = spawn_runner(case.root, case.store)
        case.approved = set()

        # The lane agent: it writes its draft as files in the directory it runs in.
        case.calls = []

        def agent(self, **kwargs):
            case.calls.append(kwargs)
            found = [number for number, goal in GOALS.items() if goal in kwargs["prompt"]]
            assert len(found) == 1, ("A drafting session must be about exactly one issue", kwargs["prompt"])
            tree = Path(kwargs["worktree"])
            test = tree / "tests/acceptance" / f"test_drafted_{found[0]}.py"
            test.parent.mkdir(parents=True, exist_ok=True)
            test.write_text(f'"""{marker(found[0], "tests")}"""\n\n\ndef test_drafted():\n    assert False\n')
            (tree / "plan.json").write_text(json.dumps({"id": marker(found[0], "plan"), "issues": []}))
            source = tree / "src/app.py"
            if source.is_file():
                source.write_text(source.read_text() + "CHANGED_BY_A_DRAFT = True\n")
            return ExecResult(0, False, "Drafted the tests and the plan.", kwargs["log_path"])

        for executor in (executors.ClaudeExecutor, executors.CodexExecutor):
            monkeypatch.setattr(executor, "run", agent)
        case.before = source_state(case)
        return case

    return build


def placeholder(case, number, lane, *, state="open"):
    case.tracker.add_issue(number, f"Decided work {number}", body(GOALS[number], PLACEHOLDER),
                           labels=[f"lane:{lane}"], state=state)


def approved(case, number, lane):
    case.tracker.add_issue(number, f"Approved work {number}", case.ready_body, labels=["ready", f"lane:{lane}"])
    case.tracker.record_label(number, "approved", "human")
    case.approved.add(number)


def implementation_starts(case):
    return [int(argv[2]) for argv in case.launched
            if argv[:2] == ["gate", "run-issue"] and int(argv[2]) in case.approved]


def draft_sessions(case):
    """Every worker the gate launched that is not an approved issue's implementation run."""
    return [argv for argv in case.launched
            if not (argv[:2] == ["gate", "run-issue"] and int(argv[2]) in case.approved)]


def ticks(case, count=1):
    from foragent.scheduler import tick

    for _ in range(count):
        report = tick(case.tick_ctx)
        assert report.skipped is None, report


def refresh(case):
    """Let the next tick read GitHub again, as a periodic tick does."""
    case.clock[0] += timedelta(minutes=5, seconds=1)


def run_session(case, argv):
    """Run a captured worker command the way its process would."""
    before = len(case.calls)
    assert cli_exit(case.main, list(argv)) == 0, ("The drafting worker must finish cleanly", argv)
    assert len(case.calls) == before + 1, ("One worker runs one lane agent session", argv)
    return case.calls[-1]


def source_state(case):
    files = {}
    for path in sorted(case.root.rglob("*")):
        relative = path.relative_to(case.root)
        if relative.parts[0] != ".git" and path.is_file():
            files[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"files": files, "head": git_command(case.root, "rev-parse", "HEAD"),
            "origin": git_command(case.history.origin, "for-each-ref", "--format=%(refname) %(objectname)")}


def landed(case, text):
    """Files outside the source tree, the origin and the fixture template that contain ``text``."""
    roots = {case.tmp.resolve(), Path.home().resolve()}
    roots |= {Path(os.environ[key]).resolve() for key in ("XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME")
              if os.environ.get(key)}
    excluded = [case.root.resolve(), case.history.origin.resolve(), (case.tmp / "git-history-template").resolve()]
    hits = set()
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file() and not any(path.resolve().is_relative_to(place) for place in excluded):
                try:
                    if text in path.read_text(errors="ignore"):
                        hits.add(path.resolve())
                except OSError:
                    continue
    return hits


def assert_draft_outside_source(case, number):
    for part in ("tests", "plan"):
        assert landed(case, marker(number, part)), f"#{number}'s draft {part} must be kept outside the source tree"
    assert source_state(case) == case.before, "Drafting must not change the checkout, its branch or origin"
    issue = case.tracker.get_issue(number)
    assert issue.body == body(GOALS[number], PLACEHOLDER), "The orchestrator, not the draft, edits the issue"
    assert "approved" not in issue.labels


def test_placeholder_issue_gets_one_session_in_its_lane_and_the_draft_lands_outside_the_source_tree(drafts):
    case = drafts()
    placeholder(case, 11, "orch")
    case.tracker.add_issue(12, "Decided work 12", body(GOALS[12], "uv run pytest -q tests/acceptance/test_feature.py"),
                           labels=["lane:orch"])
    placeholder(case, 14, "orch", state="closed")
    ticks(case, 2)
    sessions = draft_sessions(case)
    assert len(sessions) == 1, ("A placeholder issue gets exactly one drafting session, also while it runs",
                                case.launched)
    call = run_session(case, sessions[0])
    assert call.get("agent") == "fa-orch", "The issue's own lane agent drafts it"
    assert GOALS[11] in call["prompt"]
    assert_draft_outside_source(case, 11)
    for _ in range(2):
        refresh(case)
        ticks(case)
    assert draft_sessions(case) == sessions, (
        "A drafted issue is not drafted again, and real Done when or closed issues get no session", case.launched)
    assert len(case.calls) == 1
    assert implementation_starts(case) == []


def test_two_lanes_draft_at_the_same_time(drafts):
    case = drafts()
    placeholder(case, 11, "orch")
    placeholder(case, 21, "view")
    for _ in range(3):
        if len(draft_sessions(case)) >= 2:
            break
        ticks(case)
    sessions = draft_sessions(case)
    assert len(sessions) == 2, ("Both lanes must be drafting before either session finishes", case.launched)
    calls = [run_session(case, argv) for argv in sessions]
    assert {call.get("agent") for call in calls} == {"fa-orch", "fa-view"}
    for call in calls:
        number = 11 if GOALS[11] in call["prompt"] else 21
        assert call.get("agent") == ("fa-orch" if number == 11 else "fa-view"), "Each issue is drafted in its lane"
    for number in (11, 21):
        assert_draft_outside_source(case, number)


def test_a_lane_implementation_run_starts_while_its_draft_session_runs(drafts):
    case = drafts()
    placeholder(case, 11, "orch")
    ticks(case)
    sessions = draft_sessions(case)
    assert len(sessions) == 1, ("The placeholder issue's drafting session starts first", case.launched)
    approved(case, 15, "orch")
    refresh(case)
    ticks(case)
    assert implementation_starts(case) == [15], (
        "Drafting must not hold the lane's implementation queue", case.launched)
    assert draft_sessions(case) == sessions
    call = run_session(case, sessions[0])
    assert call.get("agent") == "fa-orch"
    assert_draft_outside_source(case, 11)


def test_ready_implementation_keeps_the_only_free_slot_and_the_draft_follows(drafts):
    case = drafts(max_parallel=1)
    placeholder(case, 11, "orch")
    approved(case, 15, "view")
    ticks(case)
    assert implementation_starts(case) == [15], ("A draft must not take a ready implementation's place",
                                                 case.launched)
    case.store.finish_run(15, outcome="merged")  # The implementation run's process finished it.
    case.tracker.remove_labels(15, ["running", "ready"])
    case.tracker.close_issue(15)
    for _ in range(2):
        refresh(case)
        ticks(case)
    sessions = draft_sessions(case)
    assert len(sessions) == 1, ("The waiting draft starts once a slot is free", case.launched)
    call = run_session(case, sessions[0])
    assert call.get("agent") == "fa-orch" and GOALS[11] in call["prompt"]
    assert_draft_outside_source(case, 11)
