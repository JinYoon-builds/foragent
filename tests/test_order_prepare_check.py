"""Prepare runs the written Done when commands with host permissions in a throwaway copy (#2129)."""

import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from conftest import git_command
from foragent import capacity, order_prepare_check, order_prepare_review
from foragent.config import Config
from foragent.git import GitRepo


CANDIDATE = "tests/acceptance/test_x.py"


def _run(git_history, tmp_path, commands, *, files=None):
    git_history.update({"base.txt": "base\n", ".gitignore": ".cache/\n", **(files or {})})
    git_command(git_history.clone, "fetch", "origin", "main")
    git = GitRepo(git_history.clone)
    written = {CANDIDATE: "candidate\n"}
    directory = tmp_path / "review-1"
    directory.mkdir()
    verify = lambda copy, manifest, before: order_prepare_review._integrity(  # noqa: E731
        git, copy, manifest, Config(), (), before, written)
    results = order_prepare_check.run(git, git_history.tip, None,
                                      lambda copy: order_prepare_review._place(copy, written),
                                      verify, commands, directory, 60)
    return git, directory, results


def test_runs_each_command_once_in_a_copy_with_candidates_never_in_the_checkout(git_history, tmp_path, monkeypatch):
    monkeypatch.delenv(capacity.TEST_SLOT_ENV, raising=False)
    command = ("pwd; cat base.txt tests/acceptance/test_x.py; echo run >> runs.txt; "
               f"echo slot=${capacity.TEST_SLOT_ENV}; touch made-here; exit 3")
    git, directory, results = _run(git_history, tmp_path, [command])
    assert len(results) == 1
    run = results[0]
    assert (run["command"], run["exit_code"], run["timed_out"]) == (command, 3, False)
    lines = run["output"].splitlines()
    assert Path(lines[0]).resolve() == (directory / "check").resolve()
    assert lines[1:] == ["base", "candidate", "slot=held"]
    # What the run left in its copy is reported, so it can void the measurement.
    assert run["changed"] == ["made-here", "runs.txt"]
    # The copy is thrown away; the caller's checkout never saw the run.
    assert not (directory / "check").exists()
    assert not (git.root / "made-here").exists() and not (git.root / "runs.txt").exists()
    assert command in (directory / "check.log").read_text()


def test_each_command_gets_its_own_output_and_the_tail_is_kept(git_history, tmp_path):
    long = f"python3 -c \"print('x' * {order_prepare_check.OUTPUT_TAIL + 50}); print('END')\""
    _git, _directory, results = _run(git_history, tmp_path, ["echo first", long])
    assert [run["exit_code"] for run in results] == [0, 0]
    assert results[0]["output"] == "first\n"
    assert len(results[1]["output"]) == order_prepare_check.OUTPUT_TAIL
    assert results[1]["output"].endswith("END\n") and "first" not in results[1]["output"]


@pytest.mark.parametrize("change, voided", [
    ("printf 'assert False\\n' > tests/acceptance/test_x.py", [CANDIDATE]),          # rewritten candidate
    ("cp tests/acceptance/test_x.py k; echo x > tests/acceptance/test_x.py; cp k tests/acceptance/test_x.py; rm k",
     [CANDIDATE]),                                                                  # edited and restored
    ("echo changed > base.txt", ["base.txt"]),                                       # production file
    ("rm base.txt", ["base.txt"]),                                                   # removed file
    ("mkdir -p .cache && echo kept > .cache/state", []),                             # ignored cache: allowed
])
def test_each_command_runs_in_its_own_new_copy_and_reports_what_it_changed(git_history, tmp_path, change, voided):
    """Nothing a command leaves behind reaches the next one, and every change to the copy is reported."""
    after = "cat tests/acceptance/test_x.py base.txt; test ! -e .cache/state && echo fresh"
    _git, _directory, results = _run(git_history, tmp_path, [change, after])
    assert [run["changed"] for run in results] == [voided, []]
    assert results[1]["output"] == "candidate\nbase\nfresh\n" and results[1]["exit_code"] == 0
    assert order_prepare_check.changed(results) == voided


def test_a_wrong_command_reports_its_real_exit_code_and_output(git_history, tmp_path):
    _git, _directory, results = _run(git_history, tmp_path, ["python3 missing_runner.py"])
    assert results[0]["exit_code"] not in (0, None)
    assert "missing_runner.py" in results[0]["output"]


@pytest.mark.parametrize("existing", [False, True])
def test_regenerated_gitignored_build_info_does_not_void_a_run(git_history, tmp_path, existing):
    name = "src/foragent/_build.py"
    files = {".gitignore": f".cache/\n/{name}\n"}
    if existing:
        files[name] = "GIT_SHA = 'before'\n"
    command = f"mkdir -p src/foragent; echo regenerated > {name}; exit 1"
    _git, _directory, results = _run(git_history, tmp_path, [command], files=files)
    assert results[0]["exit_code"] == 1
    assert results[0]["changed"] == []
    assert order_prepare_check.changed(results) == []


def test_the_prompt_carries_the_measured_results():
    item = {"key": "a", "title": "A", "goal": "g", "statements": ["s"], "out_of_scope": "o", "lane": "api"}
    plan = {"title": "P", "issues": [item]}
    entry = {"written": {}, "done_when": ["check"], "base_revision": "b1"}
    results = [{"command": "check", "exit_code": 1, "timed_out": False, "output": "MEASURED-OUTPUT 1 failed",
                "changed": []},
               {"command": "slow", "exit_code": None, "timed_out": True, "output": "partial", "changed": []}]
    prompt = order_prepare_review.review_prompt(plan, item, Config(), Path("."), entry, results)
    assert "MEASURED-OUTPUT 1 failed" in prompt and "`check` (exit code 1)" in prompt
    assert "`slow` (timed out)" in prompt and "partial" in prompt
    # A swap restored after the run is invisible to any after-the-fact check; the reviewer judges it.
    assert "moves, rewrites, replaces or restores files of the tree" in " ".join(prompt.split())


def test_the_reviewer_holds_only_for_major_test_gaps():
    item = {"key": "a", "title": "A", "goal": "g", "statements": ["s"], "out_of_scope": "o", "lane": "api"}
    plan = {"title": "P", "issues": [item]}
    entry = {"written": {}, "done_when": ["check"], "base_revision": "b1"}
    prompt = " ".join(order_prepare_review.review_prompt(plan, item, Config(), Path("."), entry).split())

    assert "Use verdict hold only for these major defects:" in prompt
    assert "(a) A behaviour statement has no test at all." in prompt
    assert "(b) A test passes on current main (no red)." in prompt
    assert "(c) A test fails on current main for a reason other than the missing observable behaviour" in prompt
    for invalid_red in ("import", "invented arguments", "missing tool", "zero collected tests"):
        assert invalid_red in prompt
    assert "(d) A test would pass for an implementation that does nothing for that statement" in prompt
    assert ("asserts private internals (helper names, internal layout) so that a correct implementation "
            "would fail") in prompt
    assert ("Everything else goes to notes, never holds: more break-it cases (wrong value, skipped effect, "
            "doubled effect), stronger identity checks, extra scenarios and wording.") in prompt
    assert "incomplete break-it coverage alone is not a hold reason" in prompt
    assert "If none of (a)-(e) applies, return verdict pass with any weaknesses in notes." in prompt
    assert "each statement, with its exact text" in prompt
    assert "actual counts, the measured exit code, an output excerpt and your reading of why it failed" in prompt
    assert "behaviour already passing on main" not in prompt  # Writer guidance would contradict (b).
    assert "Pass only when all of these hold" not in prompt
    assert "when the tests fall short" not in prompt


def test_a_reported_exit_code_that_contradicts_the_measured_one_is_no_pass(tmp_path):
    (tmp_path / "test_x.py").write_text("assert False\n")
    result = SimpleNamespace(error_kind="normal", timed_out=False, exit_code=0, session_id="reviewer",
                             log_path=tmp_path / "reviewer.log")
    item = {"statements": ["One delivery is reported."]}
    entry = {"written": {"test_x.py": "assert False\n"}, "done_when": ["check"]}
    run = {"command": "check", "collected": 1, "failed": 1, "exit_code": 1, "output": "AssertionError",
           "failure_kind": "assertion"}
    reply = {"verdict": "pass", "reason": "ok", "questions": [], "red": [run],
             "coverage": [{"statement": "One delivery is reported.", "tests": ["test_x.py::t"],
                           "break_it": ["wrong value"]}]}

    def outcome(code):
        measured = [{"command": "check", "exit_code": code, "timed_out": False, "output": "", "changed": []}]
        return order_prepare_review._outcome(result, reply, item, entry, [], set(), Config(), measured)

    assert outcome(1)["status"] == "reviewed"
    contradicted = outcome(2)
    assert contradicted["status"] == "interrupted" and "check" in contradicted["reason"]


PASSING = "def test_n():\n    assert True\n"
FAILING = "def test_n():\n    assert False\n"
RED = f"python3 -c \"import runpy, sys; ns = runpy.run_path('tests/acceptance/test_n.py'); ns['test_n']()\""


@pytest.mark.parametrize("candidate, command, voided", [
    (FAILING, RED, None),
    (FAILING, f"mkdir -p src/foragent; echo regenerated > src/foragent/_build.py; {RED}", None),
    # The reproduced bypass: a passing candidate rewritten to fail, then run.
    (PASSING, f"printf 'def test_n():\\n    assert False\\n' > tests/acceptance/test_n.py; {RED}",
     "tests/acceptance/test_n.py"),
    (FAILING, f"echo host >> notice.py; {RED}", "notice.py"),
    (FAILING, f"touch stray; {RED}", "stray"),
])
def test_the_reviewer_tree_stays_separate_and_untouched(git_history, tmp_path, monkeypatch, candidate, command, voided):
    """prepare's host run leaves the reviewer's tree as built, and a run that changed its own copy
    measured something else: the attempt stops before any reviewer starts."""
    from contextlib import nullcontext

    from foragent import executors, order_prepare
    from foragent.executors import ExecResult
    from foragent.state import StateStore

    git_history.update({"notice.py": "print(0)\n", ".gitignore": "/src/foragent/_build.py\n"})
    root = git_history.clone
    git_command(root, "fetch", "origin", "main")
    git_command(root, "merge", "--ff-only", "origin/main")
    name = "tests/acceptance/test_n.py"
    plan = {"id": "p", "title": "P", "summary": "s", "spec_markdown": "s", "issues": [{
        "key": "n", "title": "N", "goal": "g", "statements": ["One."], "out_of_scope": "o",
        "done_when": ["true"], "lane": "main", "size": "s", "blocked_by": []}]}
    path = root / "PLAN.json"
    path.write_text(json.dumps(plan))
    config = Config(repo="o/r", language="en", executor="codex", executor_model="w",
                    reviewer="codex", reviewer_model="r", skill_libraries=[])
    monkeypatch.chdir(root)
    monkeypatch.setattr(capacity, "hold_workload", lambda *a, **kw: nullcontext())
    seen = {}

    def run(self, **kw):
        tree = Path(kw["worktree"])
        if kw["model"] == "w":
            (tree / name).parent.mkdir(parents=True, exist_ok=True)
            (tree / name).write_text(candidate)
            message = {"acceptance_files": {name: candidate},
                       "done_when": [command], "questions": []}
        else:
            seen.update(notice=(tree / "notice.py").read_text(), stray=(tree / "stray").exists(),
                        prompt=kw["prompt"], isolated=kw.get("isolated"))
            message = {"verdict": "pass", "reason": "ok", "questions": [],
                       "coverage": [{"statement": "One.", "tests": [name], "break_it": ["wrong"]}],
                       "red": [{"command": command, "collected": 1, "failed": 1, "exit_code": 1,
                                "output": "x", "failure_kind": "assertion"}]}
        Path(kw["log_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(kw["log_path"]).write_text(json.dumps(message))
        return ExecResult(0, False, json.dumps(message), Path(kw["log_path"]), session_id=kw["model"])

    monkeypatch.setattr(executors.CodexExecutor, "run", run)
    rows = order_prepare.prepare(plan, root, config, GitRepo(root), StateStore(tmp_path / "state"), plan_path=path)
    if voided is not None:
        assert rows[0]["status"] == "interrupted" and voided in rows[0]["reason"], rows
        assert seen == {}, "No reviewer may judge a measurement of changed bytes"
        assert "acceptance_files" not in json.loads(path.read_text())["issues"][0]
        return
    assert rows[0]["status"] == "reviewed", rows
    assert seen["notice"] == "print(0)\n" and seen["stray"] is False and seen["isolated"] is True
    assert "(exit code 1)" in seen["prompt"]
    assert (root / "notice.py").read_text() == "print(0)\n" and not (root / "stray").exists()
    assert not os.path.lexists(root / "stray")


def test_measured_python_command_has_a_local_venv_and_reads_scratch_sources(git_history, tmp_path, monkeypatch):
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("UV_PYTHON", sys.executable)
    monkeypatch.setenv("UV_NO_SYNC", "1")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(tmp_path / "wrong-environment"))
    monkeypatch.setenv("UV_PROJECT", str(tmp_path / "wrong-project"))
    monkeypatch.setenv("PYTHONPATH", str(git_history.clone))
    command = '.venv/bin/python -c "import notice; print(notice.VALUE)"'
    files = {".gitignore": ".venv/\n__pycache__/\n", "notice.py": "VALUE = 'scratch main'\n",
             "pyproject.toml": '[project]\nname = "toy-prepare"\nversion = "0"\nrequires-python = ">=3.12"\n',
             "uv.lock": ('version = 1\nrevision = 3\nrequires-python = ">=3.12"\n'
                         '[[package]]\nname = "toy-prepare"\nversion = "0"\nsource = { virtual = "." }\n')}
    (git_history.clone / "notice.py").write_text("VALUE = 'dirty checkout'\n")
    git, directory, results = _run(git_history, tmp_path, [command], files=files)
    assert results[0]["exit_code"] == 0, results
    assert results[0]["output"].endswith("scratch main\n"), results
    assert results[0]["changed"] == []
    assert not (git.root / ".venv").exists() and not (directory / "check").exists()
    assert not (tmp_path / "wrong-environment").exists()


@pytest.mark.parametrize("recovers", [True, False])
def test_measured_exit_127_retries_once_and_never_becomes_a_hold(request, capsys, monkeypatch, recovers):
    from helpers import lane_tests

    case = request.getfixturevalue("lane_case")
    case.verdicts = ["hold", "pass"]
    original = order_prepare_check.run
    measured = []

    def run(*args, **kwargs):
        results = original(*args, **kwargs)
        measured.append(results)
        if len(measured) == 1 or not recovers:
            return [result | {"exit_code": 127, "output": "tool: command not found"} for result in results]
        return results

    monkeypatch.setattr(order_prepare_check, "run", run)
    row = lane_tests.prepare(case, capsys)
    assert row["status"] == ("reviewed" if recovers else "interrupted") and row["holds"] == 0
    assert len(measured) == 2
    assert [call["role"] for call in case.calls] == ["writer", "reviewer", "reviewer"]


from helpers.lane_tests import lane_case, offline_publication  # noqa: E402, F401
