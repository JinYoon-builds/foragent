"""Acceptance test for base-move-reuse. Locked by Foragent; do not edit."""

from collections import Counter
import os
from pathlib import Path
import shlex
import sys

import pytest

from conftest import ACCEPTANCE, git_command
from foragent import executors, run_checks, runner
from foragent.issue import body_hash
from test_orch_review_handoff import make_case
from test_skills_autosync import isolated_home
from test_state_split import offline_acceptance


REUSED = "base moved: only result-neutral paths; verification reused"
REVERIFY = "base moved: result-affecting paths; re-verifying"
PENDING = "tests/acceptance/test_pending.py"
MERGED = "tests/acceptance/test_merged.py"
UNOWNED = "tests/acceptance/test_unowned.py"
SHARED_DOC = "docs/shared.md"
SHARED_SOURCE = "PR section\n\none\ntwo\nthree\nfour\nfive\n\nMain section\n"
LOCKED = '"""Acceptance test for {name}. Locked by Foragent; do not edit."""\n'


def locked_test(name, *, passing=True):
    return LOCKED.format(name=name) + f"\ndef test_{name}():\n    assert {passing}\n"


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
        SHARED_DOC: SHARED_SOURCE,
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
        git_command(case.history.origin, "merge-base", "--is-ancestor", sha, branch)
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


def test_four_docs_only_moves_reuse_verification_without_timeout(publish_case, monkeypatch):
    case = publish_case
    paths = ("docs/guide.md", "docs/specs/feature.md", "docs/decisions.md", "docs/ideas/next.md")
    move_during_publish(case, monkeypatch, [[{path: "Main documentation\n"}] for path in paths])
    log = run_to_merge(case, moves=4)
    assert len(case.main_commits) == 4
    assert_verifications(case, [case.base_sha])
    assert log.count(REUSED) == 4
    assert REVERIFY not in log


@pytest.mark.parametrize("with_docs", [False, True], ids=["locked-only", "docs-and-locked"])
def test_excluded_locked_acceptance_changes_reuse_verification(
    publish_case, monkeypatch, with_docs,
):
    case = publish_case
    batch = [{PENDING: locked_test("pending", passing=False) + "\n# Approved spec clarification\n"}]
    if with_docs:
        batch.insert(0, {"docs/specs/pending.md": "Approved, still unimplemented\n"})
    move_during_publish(case, monkeypatch, [batch])
    log = run_to_merge(case, moves=1)
    assert_verifications(case, [case.base_sha])
    assert log.count(REUSED) == 1
    assert REVERIFY not in log


# Existing re-verification is a control inside a new reuse scenario: each test
# must be red on main, while still rejecting a fix that simply skips all checks.
@pytest.mark.parametrize("files", [
    pytest.param({"src/widget.py": "VALUE = 1\n"}, id="src"),
    pytest.param({"tests/test_widget.py": "def test_widget():\n    assert 1 == 1\n"}, id="unit-test"),
    pytest.param({".foragent.yml": "repo: owner/repo\nreview: careful\n"}, id="foragent-config"),
    pytest.param({"pyproject.toml": '[project]\nname = "offline-feature"\nversion = "0.1.0"\n'},
                 id="pyproject"),
    pytest.param({MERGED: locked_test("merged") + "\n# Already merged owner\n"}, id="merged-locked"),
    pytest.param({UNOWNED: locked_test("unowned") + "\n# No approved owner\n"}, id="unowned-locked"),
    pytest.param({"docs-notes.md": "Outside docs/\n"}, id="docs-prefix-is-not-directory"),
    pytest.param({"README.md": "Outside docs/\n"}, id="other-path"),
])
def test_result_affecting_move_reverifies_after_neutral_move(publish_case, monkeypatch, files):
    case = publish_case
    move_during_publish(case, monkeypatch, [
        [{"docs/guide.md": "Result-neutral first move\n"}], [files],
    ])
    log = run_to_merge(case, moves=2)
    assert case.verifications.count(case.moves[1]) == 1, "A result-affecting move must re-verify once"
    assert_verifications(case, [case.base_sha, case.moves[1]])
    assert log.count(REUSED) == log.count(REVERIFY) == 1


@pytest.mark.parametrize("revert_src", [False, True], ids=["docs-plus-src", "src-changed-then-reverted"])
def test_mixed_commit_batch_reverifies_once(publish_case, monkeypatch, revert_src):
    case = publish_case
    batch = [{"docs/specs/feature.md": "Documentation in the same fetched batch\n"},
             {"src/widget.py": "VALUE = 1\n"}]
    if revert_src:
        batch.append({"src/widget.py": "VALUE = 0\n"})
    move_during_publish(case, monkeypatch, [
        [{"docs/guide.md": "An earlier result-neutral move\n"}], batch,
    ])
    log = run_to_merge(case, moves=2)
    assert len(case.main_commits) == 1 + len(batch)
    if revert_src:
        assert git_command(case.history.origin, "diff", "--name-only", case.moves[0], case.moves[1]) == (
            "docs/specs/feature.md"
        ), "Even a net docs-only diff must inspect every intervening commit"
    assert case.verifications.count(case.moves[1]) == 1, "A mixed batch re-verifies once, not per commit"
    assert_verifications(case, [case.base_sha, case.moves[1]])
    assert log.count(REUSED) == log.count(REVERIFY) == 1


def test_main_change_to_prs_own_doc_reverifies(publish_case, monkeypatch):
    case = publish_case

    def implement(kwargs):
        case.implement(kwargs)
        (kwargs["worktree"] / SHARED_DOC).write_text(SHARED_SOURCE.replace("PR section", "PR updated"))
        return "Implemented feature and its documentation"

    case.implementer.steps = [implement]
    move_during_publish(case, monkeypatch, [
        [{"docs/guide.md": "Unrelated, result-neutral documentation\n"}],
        [{SHARED_DOC: SHARED_SOURCE.replace("Main section", "Main updated")}],
    ])
    log = run_to_merge(case, moves=2)
    published = git_command(case.history.origin, "show", f"refs/heads/foragent/issue-1:{SHARED_DOC}")
    assert "PR updated" in published and "Main updated" in published, "Both non-conflicting edits must survive"
    assert case.verifications.count(case.moves[1]) == 1, "Main touched a path changed by this PR"
    assert_verifications(case, [case.base_sha, case.moves[1]])
    assert log.count(REUSED) == log.count(REVERIFY) == 1
