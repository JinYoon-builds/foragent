"""Publication-loop cases for result-neutral base moves (#376)."""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))

from foragent.issue import body_hash  # noqa: E402
from test_base_move_reuse import (  # noqa: E402,F401
    REUSED, REVERIFY, UNOWNED, assert_verifications, locked_test, move_during_publish, publish_case,
    run_to_merge,
)
from test_skills_autosync import isolated_home  # noqa: E402,F401
from test_state_split import offline_acceptance  # noqa: E402,F401


def test_more_neutral_moves_than_publication_rounds_still_merge(publish_case, monkeypatch):
    case = publish_case
    move_during_publish(case, monkeypatch, [[{f"docs/note-{index}.md": "Docs\n"}] for index in range(6)])
    log = run_to_merge(case, moves=6)
    assert_verifications(case, [case.base_sha])
    assert log.count(REUSED) == 6
    assert REVERIFY not in log


def test_issue_approved_after_verification_does_not_make_its_test_neutral(publish_case, monkeypatch):
    case = publish_case
    move_during_publish(case, monkeypatch, [[{UNOWNED: locked_test("unowned") + "\n# Late owner\n"}]])
    review = case.reviewer.steps[0]

    def approve_then_review(kwargs):
        # The baseline already ran this file; a later approval must not hide changes to it.
        approval = case.store.load_approval(1)
        case.tracker.add_issue(4, f"Own {UNOWNED}", approval.body, labels=["approved"])
        case.store.save_approval(
            4, body=approval.body, body_sha=body_hash(approval.body),
            acceptance_blobs=case.git.blob_shas("origin/main", [UNOWNED]),
            approved_by="human", approved_at=case.clock[0].isoformat(),
        )
        return review(kwargs)

    case.reviewer.steps = [approve_then_review]
    log = run_to_merge(case, moves=1)
    assert_verifications(case, [case.base_sha, case.moves[0]])
    assert log.count(REVERIFY) == 1
    assert REUSED not in log


def test_merge_uses_the_classified_commit_when_the_shared_ref_moves_again(publish_case, monkeypatch):
    """Another fetch may advance origin/main between classification and merge."""
    from foragent import run_publish
    from conftest import git_command

    case = publish_case
    move_during_publish(case, monkeypatch, [[{"docs/guide.md": "Result-neutral move\n"}]])
    classify = run_publish.base_move_is_neutral
    late = []

    def classify_then_race(attempt, move):
        neutral = classify(attempt, move)
        if not late:
            case.history.update({"src/widget.py": "VALUE = 1\n"})
            late.append(case.history.tip)
            case.main_commits.append(case.history.tip)
            case.ctx.git.fetch("origin", "main")
            assert git_command(case.ctx.repo_root, "rev-parse", "origin/main") == late[0]
        return neutral

    monkeypatch.setattr(run_publish, "base_move_is_neutral", classify_then_race)
    log = run_to_merge(case, moves=1)
    assert case.verifications.count(late[0]) == 1, "The racing src commit must be verified before publication"
    assert_verifications(case, [case.base_sha, late[0]])
    assert log.count(REUSED) == log.count(REVERIFY) == 1
