"""Acceptance test for baseline-follows-base. Locked by Foragent; do not edit."""

from collections import Counter
import shlex

import pytest

from conftest import ACCEPTANCE, git_command
from foragent import run_baseline, runner
from foragent.issue import body_hash
from foragent.reasons import Reason, parse_reason
from test_base_move_reuse import (  # noqa: F401
    LOCKED, MERGED, PENDING, REVERIFY, UNOWNED,
    assert_verifications, locked_test, move_during_publish, publish_case,
)
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401


OLD_PASS = f"{UNOWNED}::test_unowned"
STILL_PASS = f"{MERGED}::test_merged"
UNCOLLECTABLE = "tests/acceptance/test_uncollectable.py"
COMPATIBILITY = (
    LOCKED.format(name="merged")
    + "from pathlib import Path\n\n"
    "def test_merged():\n"
    "    required = Path('required-version.txt').read_text().strip()\n"
    "    assert required in Path('supported-versions.txt').read_text().splitlines()\n"
)


@pytest.fixture
def follows_case(publish_case, monkeypatch):
    """Real baseline capture, cache, verification and Git; reuse offline executors.

    The PR may drop v2 support while the old base still requires v1. Only
    merging the moved base's v2 requirement can expose that regression.
    Observe subprocesses without replacing their execution or JUnit output.
    """
    case = publish_case
    case.history.update({
        MERGED: COMPATIBILITY,
        "required-version.txt": "v1\n",
        "supported-versions.txt": "v1\nv2\n",
    })
    git_command(case.root, "fetch", "-q", "origin")
    git_command(case.root, "merge", "-q", "--ff-only", "origin/main")
    case.base_sha = case.history.tip
    case.captures, case.protected = [], []
    process = runner.run_logged

    def observe(argv, *, cwd, **kwargs):
        words = shlex.split(argv[2])
        if any(word.startswith("--junitxml=") for word in words):
            case.captures.append((git_command(cwd, "rev-parse", "HEAD"), words))
        selectors = {word for word in words if "::" in word}
        checking = cwd == case.store.worktree_path(1) and "pytest" in words and selectors
        base = git_command(cwd, "rev-parse", "origin/main") if checking else None
        result = process(argv, cwd=cwd, **kwargs)
        if checking:
            case.protected.append((base, selectors, result[0]))
        return result

    monkeypatch.setattr(runner, "run_logged", observe)
    return case


def approve_changed_test(case):
    """An issue approved after the old baseline owns the changed locked file."""
    body = case.store.load_approval(1).body
    case.tracker.add_issue(4, "Implement the changed acceptance contract", body, labels=["approved"])
    case.store.save_approval(
        4, body=body, body_sha=body_hash(body),
        acceptance_blobs={UNOWNED: git_command(case.history.origin, "rev-parse",
                                             f"{case.history.tip}:{UNOWNED}")},
        approved_by="human", approved_at=case.clock[0].isoformat(),
    )


def assert_initial_protection(case):
    assert case.protected[0] == (case.base_sha, {OLD_PASS, STILL_PASS}, 0), (
        "Both acceptance IDs must pass and be protected before main moves"
    )
    assert {word for word in case.captures[0][1] if word.startswith("--ignore=")} == {
        f"--ignore={ACCEPTANCE}", f"--ignore={PENDING}",
    }
    assert len(case.implementer.calls) == len(case.reviewer.calls) == 1


def assert_refreshed_merge(case, outcome):
    assert outcome == "merged", (
        f"A failure already on the new base must not be this PR's regression; got {outcome}"
    )
    assert len(case.moves) == 1
    moved, = case.moves
    assert_verifications(case, [case.base_sha, moved])
    assert_initial_protection(case)
    refreshed = [(ids, code) for base, ids, code in case.protected if base == moved]
    assert refreshed == [({STILL_PASS}, 0)], (
        "Re-verification must protect exactly the passing IDs from the new base"
    )
    assert case.tracker.created_prs == case.tracker.merged_prs
    assert len(case.tracker.merged_prs) == 1
    assert case.tracker.get_issue(1).state == "closed"
    assert case.store.load_flag(1) is None
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human"}
    assert all(OLD_PASS not in comment.body for comment in case.tracker.list_comments(1)), (
        "The new base's failing ID must not be reported as this PR's regression"
    )
    branch = "refs/heads/foragent/issue-1"
    git_command(case.history.origin, "merge-base", "--is-ancestor", moved, branch)
    assert UNOWNED not in git_command(
        case.history.origin, "diff", "--name-only", moved, branch,
    ).splitlines(), "The PR must not change the failing acceptance test"
    assert REVERIFY in case.store.log_path(1).read_text()


@pytest.mark.parametrize("new_owner", [False, True], ids=["fails-on-new-base", "new-open-issue"])
def test_new_base_failure_leaves_protected_set(follows_case, monkeypatch, new_owner):
    case = follows_case
    move_during_publish(case, monkeypatch, [[{UNOWNED: locked_test("unowned", passing=False)}]])
    if new_owner:
        review = case.reviewer.steps[0]

        def review_and_approve(kwargs):
            verdict = review(kwargs)
            approve_changed_test(case)
            return verdict

        case.reviewer.steps = [review_and_approve]

    outcome = runner.run_issue(1, case.ctx)

    assert_refreshed_merge(case, outcome)
    assert [sha for sha, _ in case.captures] == [case.base_sha, case.moves[0]], (
        "A cold moved-base baseline must be captured exactly once at its SHA"
    )
    excluded = {word for word in case.captures[-1][1] if word.startswith("--ignore=")}
    assert excluded == {
        f"--ignore={ACCEPTANCE}", f"--ignore={PENDING}",
        *([f"--ignore={UNOWNED}"] if new_owner else []),
    }, "Open-issue exclusions must be derived again for the new baseline"


def test_pr_regression_on_new_base_still_refuses_merge(follows_case, monkeypatch):
    """Control: today's retries-exceeded path must continue to hold."""
    case = follows_case

    def remove_v2_support(kwargs):
        case.implement(kwargs)
        (kwargs["worktree"] / "supported-versions.txt").write_text("v1\n")

    case.implementer.steps = [remove_v2_support]
    move_during_publish(case, monkeypatch, [[{
        UNOWNED: locked_test("unowned", passing=False),
        "required-version.txt": "v2\n",
    }]])

    outcome = runner.run_issue(1, case.ctx)

    assert outcome == "retries-exceeded", (
        f"Breaking an ID that passes on the new base must still fail the run; got {outcome}"
    )
    assert_initial_protection(case)
    moved, = case.moves
    assert_verifications(case, [case.base_sha, moved])
    assert git_command(case.history.origin, "show", f"{moved}:required-version.txt") == "v2"
    assert "v2" in git_command(
        case.history.origin, "show", f"{moved}:supported-versions.txt",
    ).splitlines(), "The compatibility test still passes on the new base itself"
    refreshed = [(ids, code) for base, ids, code in case.protected if base == moved]
    assert refreshed and all(STILL_PASS in ids and code == 1 for ids, code in refreshed)
    assert f"FAILED {STILL_PASS}" in case.store.log_path(1).read_text()
    assert case.tracker.merged_prs == []
    assert case.tracker.get_issue(1).state == "open"
    assert case.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED
    assert case.store.get_run(1).attempts == 1


def test_moved_base_reuses_prebuilt_baseline_without_recapture(follows_case, monkeypatch):
    case = follows_case
    move_during_publish(case, monkeypatch, [[{UNOWNED: locked_test("unowned", passing=False)}]])
    review = case.reviewer.steps[0]

    def review_and_prebuild(kwargs):
        verdict = review(kwargs)
        case.git.fetch("origin", "main")
        # Run #325's actual producer synchronously; no background threads,
        # fake JUnit, cache seeding or replacement baseline consumer.
        run_baseline.prebuild_acceptance_baseline(
            case.ctx, "origin/main", run_baseline._build_baseline,
        )
        assert Counter(sha for sha, _ in case.captures) == Counter([case.base_sha, case.moves[0]])
        assert len(list((case.store.root.parent / "baselines").glob("*.json"))) == 2, (
            "Both SHA-specific baselines must be usable before publication resumes"
        )
        return verdict

    case.reviewer.steps = [review_and_prebuild]

    outcome = runner.run_issue(1, case.ctx)

    assert "cache=hit" in case.store.log_path(1).read_text(), (
        "Publication must consume the prebuilt baseline for the moved base"
    )
    assert [sha for sha, _ in case.captures] == [case.base_sha, case.moves[0]], (
        "The moved base's prebuilt baseline must be reused without another capture"
    )
    assert_refreshed_merge(case, outcome)


def test_unusable_new_base_baseline_stops_with_327_outcome(follows_case, monkeypatch):
    case = follows_case
    # The old selectors remain green. Only a real new-base capture discovers
    # this collection error; stale-baseline verification incorrectly merges.
    move_during_publish(case, monkeypatch, [[{UNCOLLECTABLE: "def test_uncollectable(:\n"}]])

    outcome = runner.run_issue(1, case.ctx)

    assert outcome == "acceptance-baseline-unavailable", (
        f"An unusable moved-base baseline must stop exactly as #327 does; got {outcome}"
    )
    assert_initial_protection(case)
    assert case.tracker.merged_prs == []
    assert case.tracker.get_issue(1).state == "open"
    assert case.store.load_flag(1).reason == Reason.ACCEPTANCE_BASELINE_UNAVAILABLE
    assert "acceptance baseline unavailable" in case.store.get_run(1).detail
    assert parse_reason(case.tracker.list_comments(1)[-1].body) == Reason.ACCEPTANCE_BASELINE_UNAVAILABLE
    assert case.store.read_history()[-1].outcome == "acceptance-baseline-unavailable"
    assert case.store.get_run(1).attempts == 1
    assert [sha for sha, _ in case.captures] == [case.base_sha, case.moves[0]]
    assert all(base == case.base_sha for base, _, _ in case.protected), (
        "Do not verify the moved base using stale protection after capture fails"
    )
    assert len(list((case.store.root.parent / "baselines").glob("*.json"))) == 1, (
        "The unusable new-base capture must not be cached"
    )
