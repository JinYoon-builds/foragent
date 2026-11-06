"""Shared offline helpers for unit tests."""

from collections import Counter
import json
import os
from pathlib import Path
import shlex
import sys

import pytest

from conftest import ACCEPTANCE, git_command
from foragent import executors, run_checks, runner
from foragent.issue import body_hash

from helpers.home import offline_acceptance
from helpers.review_case import make_case


REUSED = "base moved: only result-neutral paths; verification reused"
REVERIFY = "base moved: result-affecting paths; re-verifying"
PENDING = "tests/acceptance/test_pending.py"
MERGED = "tests/acceptance/test_merged.py"
UNOWNED = "tests/acceptance/test_unowned.py"
_SHARED_DOC = "docs/shared.md"
_SHARED_SOURCE = "PR section\n\none\ntwo\nthree\nfour\nfive\n\nMain section\n"
_LOCKED = '"""Acceptance test for {name}. Locked by Foragent; do not edit."""\n'


def locked_test(name, *, passing=True):
    return _LOCKED.format(name=name) + f"\ndef test_{name}():\n    assert {passing}\n"


def squash_main(history, head):
    """Fake GitHub's squash tree on current main, including a divergent base."""
    tree = git_command(history.origin, "merge-tree", "--write-tree", history.tip, head).splitlines()[0]
    stream = ["commit refs/heads/main\n", "committer Tests <tests@example.com> 1792483200 +0000\n",
              "data 6\nSquash\n", f"from {history.tip}\n", "deleteall\n"]
    for entry in git_command(history.origin, "ls-tree", "-r", "-z", tree).split("\0"):
        if entry:
            metadata, name = entry.split("\t", 1)
            mode, kind, sha = metadata.split()
            stream.append(f"M {mode} {sha} {json.dumps(name)}\n")
    git_command(history.origin, "fast-import", "--quiet", input="".join(stream) + "\ndone\n")
    history.tip = git_command(history.origin, "rev-parse", "main")


@pytest.fixture
def publish_case(git_history, tmp_path, monkeypatch, offline_acceptance):
    """Real runner/Git/checks; only tracker and model executors are fakes."""
    def forbidden_signal(*args, **kwargs):
        raise AssertionError(f"Acceptance tests must not signal processes: {args}")

    def forbidden_model(*args, **kwargs):
        raise AssertionError("Acceptance tests must not start real model turns")

    monkeypatch.setattr(os, "kill", forbidden_signal)
    monkeypatch.setattr(os, "killpg", forbidden_signal)
    monkeypatch.setattr(executors.ClaudeExecutor, "run", forbidden_model)
    monkeypatch.setattr(executors.CodexExecutor, "run", forbidden_model)
    git_history.update({
        "src/widget.py": "VALUE = 0\n",
        "tests/test_widget.py": "def test_widget():\n    assert True\n",
        ".foragent.yml": "repo: owner/repo\nreview: fast\n",
        "pyproject.toml": '[project]\nname = "offline-feature"\nversion = "0.0.0"\n',
        _SHARED_DOC: _SHARED_SOURCE,
        PENDING: locked_test("pending", passing=False),
        MERGED: locked_test("merged"),
        UNOWNED: locked_test("unowned"),
    })
    git_command(git_history.clone, "fetch", "-q", "origin")
    git_command(git_history.clone, "merge", "-q", "--ff-only", "origin/main")
    case = make_case(
        git_history, tmp_path, monkeypatch, skill_libraries=[], max_retries=0,
        test_command=f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider",
    )
    case.base_sha = git_history.tip
    approval = case.store.load_approval(1)
    for number, path in ((2, PENDING), (3, MERGED)):
        case.tracker.add_issue(number, f"Own {path}", approval.body, labels=["approved"])
        case.store.save_approval(
            number, body=approval.body, body_sha=body_hash(approval.body),
            acceptance_blobs=case.git.blob_shas("origin/main", [path]),
            approved_by="human", approved_at=case.clock[0].isoformat(),
        )
    case.store.start_run(3, branch="foragent/issue-3", worktree=case.store.worktree_path(3))
    case.store.finish_run(3, outcome="merged")
    case.tracker.close_issue(3)

    # Spy on both existing imports of the verification entry, without replacing
    # its checks, acceptance baseline, publication checks or merge decisions.
    case.verifications = []
    verify = run_checks._verify_implementation

    def observe_verification(attempt):
        case.verifications.append(attempt.git.ref_sha("origin/main"))
        return verify(attempt)

    monkeypatch.setattr(runner, "_verify_implementation", observe_verification)
    monkeypatch.setattr(run_checks, "_verify_implementation", observe_verification)
    case.commands = []
    run_logged = runner.run_logged

    def observe_process(argv, *, cwd, **kwargs):
        assert argv[:2] == ["/bin/sh", "-c"], argv
        case.commands.append((Path(cwd), shlex.split(argv[2])))
        return run_logged(argv, cwd=cwd, **kwargs)

    monkeypatch.setattr(runner, "run_logged", observe_process)
    return case


def move_during_publish(case, monkeypatch, batches):
    """Each batch is one observed base move, possibly containing many commits.

    The fake reviewer delivers the first batch after initial verification.
    Later batches arrive at successive fake CI polls. Real fetch/refresh/merge
    code therefore sees changes during a single publication, without threads,
    sleeps, private refresh hooks, real signals or real model turns.
    """
    pending = iter(batches)
    case.moves, case.main_commits = [], []

    def advance():
        batch = next(pending, None)
        if batch is not None:
            for files in batch:
                case.history.update(files)
                case.main_commits.append(case.history.tip)
            case.moves.append(case.history.tip)

    def review(kwargs):
        assert case.verifications == [case.base_sha], "Main must move after initial verification"
        advance()
        return "VERDICT: PASS"

    checks = case.tracker.pr_checks

    def poll(pr):
        status = checks(pr)
        advance()
        return status

    case.reviewer.steps = [review]
    monkeypatch.setattr(case.tracker, "pr_checks", poll)


def run_to_merge(case, *, moves):
    outcome = runner.run_issue(1, case.ctx)
    log = case.store.log_path(1).read_text()
    assert outcome == "merged", (
        f"Result-neutral base moves must not exhaust publish's moving-base budget: "
        f"expected merged, got {outcome}; observed {len(case.moves)} moves and "
        f"{len(case.verifications) - 1} extra verification runs\n{log}"
    )
    assert len(case.moves) == moves, "The complete main-movement scenario must be exercised"
    assert len(case.tracker.created_prs) == len(case.tracker.merged_prs) == 1
    assert case.tracker.get_issue(1).state == "closed"
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human"}
    assert case.store.load_flag(1) is None
    assert "base kept moving" not in log
    assert len(case.implementer.calls) == len(case.reviewer.calls) == 1

    branch = "refs/heads/foragent/issue-1"
    for sha in case.main_commits:
        git_command(case.history.origin, "merge-base", "--is-ancestor", sha, "main")
    git_command(case.history.origin, "merge-tree", "--write-tree", "main", branch)
    published = git_command(case.history.origin, "rev-parse", branch)
    assert case.tracker.pr_head_sha(case.tracker.merged_prs[0]) == published

    # Establish the actual baseline selection, rather than faking ownership or
    # teaching the test a separate classification algorithm.
    captures = [words for _, words in case.commands
                if any(word.startswith("--junitxml=") for word in words)]
    assert captures, "The real acceptance baseline must run before implementation"
    assert {word for word in captures[0] if word.startswith("--ignore=")} == {
        f"--ignore={ACCEPTANCE}", f"--ignore={PENDING}",
    }, "Only approved, unmerged owners' locked files are excluded"
    protected = [words for cwd, words in case.commands
                 if cwd == case.store.worktree_path(1) and "pytest" in words]
    assert protected, "The initial implementation must verify the passing acceptance baseline"
    assert any(f"{MERGED}::test_merged" in words and f"{UNOWNED}::test_unowned" in words
               for words in protected)
    return log


def assert_verifications(case, expected):
    assert Counter(case.verifications) == Counter(expected), (
        "Expected verification only on the original base and result-affecting moves; "
        f"expected {len(expected) - 1} extra verification runs, "
        f"got {len(case.verifications) - 1}; "
        f"expected bases={expected}, observed bases={case.verifications}"
    )
