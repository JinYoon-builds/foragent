"""Shared offline helpers for unit tests."""

from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from conftest import git_command

from helpers.home import cli_exit, isolated_home
from helpers.review_case import bind_cli, make_case, tick_context


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


def _marker(number, part):
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
            test.write_text(f'"""{_marker(found[0], "tests")}"""\n\n\ndef test_drafted():\n    assert False\n')
            (tree / "plan.json").write_text(json.dumps({"id": _marker(found[0], "plan"), "issues": []}))
            source = tree / "src/app.py"
            if source.is_file():
                source.write_text(source.read_text() + "CHANGED_BY_A_DRAFT = True\n")
            return ExecResult(0, False, "Drafted the tests and the plan.", kwargs["log_path"])

        for executor in (executors.ClaudeExecutor, executors.CodexExecutor):
            monkeypatch.setattr(executor, "run", agent)
        case.before = _source_state(case)
        return case

    return build


def placeholder(case, number, lane, *, state="open"):
    case.tracker.add_issue(number, f"Decided work {number}", body(GOALS[number], PLACEHOLDER),
                           labels=[f"lane:{lane}"], state=state)


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


def _source_state(case):
    files = {}
    for path in sorted(case.root.rglob("*")):
        relative = path.relative_to(case.root)
        if relative.parts[0] != ".git" and path.is_file():
            files[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"files": files, "head": git_command(case.root, "rev-parse", "HEAD"),
            "origin": git_command(case.history.origin, "for-each-ref", "--format=%(refname) %(objectname)")}


def _landed(case, text):
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
        assert _landed(case, _marker(number, part)), f"#{number}'s draft {part} must be kept outside the source tree"
    assert _source_state(case) == case.before, "Drafting must not change the checkout, its branch or origin"
    issue = case.tracker.get_issue(number)
    assert issue.body == body(GOALS[number], PLACEHOLDER), "The orchestrator, not the draft, edits the issue"
    assert "approved" not in issue.labels
