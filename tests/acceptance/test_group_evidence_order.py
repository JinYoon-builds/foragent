"""Locked acceptance for group-evidence. Do not edit during implementation.

Break-it: treating absent baselines, unknown exit codes, timeouts or unreadable
results as PASS fails. Head equality alone, a net-diff-only base check, or
attributing an intervening outsider to the previous member also fails.
"""

from dataclasses import replace
from pathlib import Path

import pytest

from foragent import runner
from test_merge_group import (  # noqa: F401
    contend, group_case, isolated_home, offline_acceptance, prepare, publish_case,
)


def joint_rows(case):
    return [row for row in case.probes if row.armed and row.before_merges == 0
            and row.present == frozenset(case.members) and "tools/group_probe.py" in row.command]


def assert_solo_followup(case, n):
    own = case.store.worktree_path(n)
    assert any(row.actor == n and row.worktree == own and row.before_merges > 0
               and "tools/group_probe.py" in row.command for row in case.probes), (
        "Invalid group evidence requires the follower's ordinary one-PR recheck"
    )


def test_absent_baseline_is_not_a_successful_empty_baseline(group_case):
    case = group_case
    # Positive control makes this safety test red on main's one-PR-only path.
    prepare(case, (1, 4))
    contend(case)
    assert joint_rows(case), "Positive control: a complete baseline must allow a joint check"
    assert all(case.outcomes[n] == "merged" for n in (1, 4))
    case.armed = False
    case.probes.clear()
    case.merges.clear()
    # A supported ordinary shell check deliberately supplies no pytest baseline.
    case.ctx.config = replace(case.ctx.config, test_command="test -f tools/group_probe.py")
    prepare(case, (60, 70))
    contend(case)
    assert all("Acceptance baseline skipped" in case.store.log_path(n).read_text() for n in (60, 70))
    assert [row.issue for row in case.merges] == [60, 70]
    assert all(case.outcomes[n] == "merged" for n in (60, 70))
    assert_solo_followup(case, 70)


@pytest.mark.parametrize("failure", ["unknown-exit", "timeout", "unreadable-result"])
def test_unusable_joint_result_falls_back_instead_of_authorizing_a_merge(group_case, monkeypatch, failure):
    case = group_case
    prepare(case, (1, 4))
    process = runner.run_logged
    injected = []

    def unavailable(argv, *, cwd, **kwargs):
        root = Path(cwd)
        if (not injected and case.armed and not case.merges
                and root not in {case.store.worktree_path(n) for n in case.members}
                and "tools/group_probe.py" in argv[-1]
                and all((root / f"src/member_{n}.py").exists() for n in case.members)):
            injected.append(failure)
            if failure == "unreadable-result":
                raise OSError("Offline joint result could not be read")
            return (None, False, 0) if failure == "unknown-exit" else (0, True, 0)
        return process(argv, cwd=cwd, **kwargs)

    monkeypatch.setattr(runner, "run_logged", unavailable)
    contend(case)
    assert injected == [failure], "Missing joint check: the unavailable-result scenario was never exercised"
    assert [row.issue for row in case.merges] == [1, 4]
    assert all(case.outcomes[n] == "merged" for n in (1, 4)), "A void group must recover through normal publication"
    assert_solo_followup(case, 4)
    assert all(actor == n for actor, n in case.record_writes)


def test_changed_head_cannot_spend_the_old_joint_pass(group_case):
    case = group_case
    prepare(case, (1, 4))
    changed = []

    def replace_head(n):
        if n == 1:
            with case.external():
                pr = case.tracker.find_pr("foragent/issue-4")
                case.tracker.set_pr_head_sha(pr, "f" * 40)
                changed.append(pr)

    case.on_merged = replace_head
    contend(case)
    assert joint_rows(case), "Head invalidation must exercise an actual earlier joint check"
    assert changed
    assert [row.issue for row in case.merges] == [1]
    assert case.outcomes[4] == "held", "Unverified replacement heads use the normal changed-head hold"
    assert case.tracker.get_issue(4).state == "open"


def test_new_passing_main_test_remains_protected_after_the_group_becomes_stale(group_case):
    case = group_case
    prepare(case, (1, 4))
    guard = "tests/acceptance/test_new_main_guard.py"

    def protect_current_main(n):
        if n == 1:
            with case.external():
                case.history.update({guard: (
                    "from pathlib import Path\n\ndef test_new_main_guard():\n"
                    "    assert not Path('src/member_4.py').exists()\n"
                )})

    case.on_merged = protect_current_main
    contend(case)
    assert joint_rows(case), "Baseline refresh must follow a real earlier joint check"
    assert [row.issue for row in case.merges] == [1], "A newly passing main test must block its regression"
    assert case.outcomes[4] == "retries-exceeded"
    assert case.tracker.get_issue(4).state == "open"
    assert any(row.actor == 4 and row.before_merges and guard in row.command
               and row.result.exit_code != 0 for row in case.probes), (
        "Refresh protection from current main instead of keeping only the earlier baseline"
    )


@pytest.mark.parametrize("move", ["source", "reverted-source", "during-member-merge"])
def test_outsider_in_the_base_interval_invalidates_reuse(group_case, monkeypatch, move):
    case = group_case
    prepare(case, (1, 4))
    injected = []

    def advance():
        with case.external():
            case.history.update({"src/outsider.py": "VALUE = 1\n"})
            if move == "reverted-source":
                case.history.update({"src/outsider.py": None, "docs/net-neutral.md": "Notes\n"})
            injected.append(case.history.tip)

    if move == "during-member-merge":
        merge = case.tracker.merge_pr

        def merge_after_outsider(pr, **kwargs):
            # A push after the final read must not be laundered into this
            # member's provenance when its resulting main commit is observed.
            if case.worker.number == 1 and not injected:
                advance()
            return merge(pr, **kwargs)

        monkeypatch.setattr(case.tracker, "merge_pr", merge_after_outsider)
    else:
        case.on_merged = lambda n: advance() if n == 1 else None
    contend(case)
    assert joint_rows(case), "Base invalidation must exercise an actual earlier joint check"
    assert len(injected) == 1
    assert [row.issue for row in case.merges] == [1, 4]
    assert all(case.outcomes[n] == "merged" for n in (1, 4))
    assert_solo_followup(case, 4)
