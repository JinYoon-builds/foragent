"""Acceptance (checked-pr-lands-first, PM r1438).

merge-owner.log 11-09: '#2878 checks end ok', then order publications and a role
main write applied to main, then '#2878 merge owner advanced' and a new owner
check, three times. Within one owner pass, a pull request whose checks on the
current main ended ok lands first; queued order publications and main write
requests apply after it, on the main it produced. A queued write never makes the
owner re-check work it already checked.

World: checked_first_world.py on the locked merge-path world (`proto`): real gate
tick, owner, runner, Git and state; fake GitHub, models and clock. The memory
write is real (`main_write_requests.capture` + the owner's drain); the order
publication is a stand-in at `order_publications.apply_next` (see the world).

Scenario: PR #5 is refreshed by the owner onto a moved main M; its owner checks
end ok at head H on M and its CI passes. Then a publication and/or a memory write
is queued and the gate runs the next owner pass.

Every test here fails on current main, where the pass applies the queued writes
first, moves main past M and refreshes #5 to a new head with a new owner check.
Regression controls (no ready PR: writes apply as today) live in
test_checked_pr_lands_first_guard.py, outside Done when.

Break-it: a write applied before the merge fails the "merge lands on M" and
"no re-check" checks; a skipped write fails the "applies after" checks; a doubled
write or merge fails the one-commit and one-merge counts.
"""

import pytest

from checked_first_world import (
    MEMORY, MEMORY_TEXT, NEIGHBOR, PUBLICATION, PUBLICATION_TEXT, check_starts, checked_single_pr, commits_touching,
    first_parent, install, is_ancestor, issue_heads, landed_merge, main_text, main_tip, memory_status,
    queue_memory, queue_publication, support,
)
from foragent import main_write_requests as writes
from helpers.merge_world import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401

QUEUED = ["memory", "publication", "both"]


def queue(case, which):
    row = None
    if which in {"publication", "both"}:
        queue_publication(case)
    if which in {"memory", "both"}:
        row = queue_memory(case)
    return row


def writes_left(case, which, row):
    left = []
    if row is not None and memory_status(case, row) != "done":
        left.append("memory")
    if which in {"publication", "both"} and not case.publication["applied_on"]:
        left.append("publication")
    return left


# ---------------------------------------------------------------- statement 1

@pytest.mark.parametrize("which", QUEUED)
def test_checked_pr_merges_on_its_checked_main_without_a_new_check(proto, monkeypatch, capsys, which):
    case = proto
    install(case, monkeypatch)
    pr, head, checked = checked_single_pr(case, capsys)
    heads = issue_heads(case, 5)
    queue(case, which)

    support.gate(case)  # The next owner pass, with the queued writes waiting.
    out = capsys.readouterr().out
    assert check_starts(out, 5) == [], (
        f"#5's checks on the current main ended ok; a queued {which} write must not make the owner "
        f"check it again; log: {out.splitlines()}")
    merged = landed_merge(case, pr)
    assert merged is not None, (
        f"#5 is checked on the current main and must merge in this pass, before the queued {which} write; "
        f"merged PRs: {case.tracker.merged_prs}")
    assert merged["head"] == head, "#5 merges with the head its checks ran on, not a refreshed head"
    assert issue_heads(case, 5) == heads, "No new head (and no new CI round) is pushed for #5"
    assert first_parent(case, merged["main"]) == checked, (
        f"#5 lands directly on the main its checks ran on ({checked[:12]}); a queued {which} write "
        "must not land on main before it")
    assert len(case.merges) == 1 and case.tracker.merged_prs == [pr.number], "#5 merges exactly once"
    assert main_text(case, "src/member_5.py") == "VALUE = 5"


def test_checked_pr_lands_first_in_every_pass_the_log_showed(proto, monkeypatch, capsys):
    """merge-owner.log showed the same loop three times: writes queued again do not reorder it."""
    case = proto
    install(case, monkeypatch)
    pr, head, checked = checked_single_pr(case, capsys)
    queue(case, "both")
    for again in range(3):
        support.gate(case)
        if landed_merge(case, pr):
            break
        queue_publication(case)  # The writes keep arriving, as on 11-09.
        queue_memory(case, f"{MEMORY_TEXT} again {again}")
    out = capsys.readouterr().out
    assert check_starts(out, 5) == [], f"#5 is never re-checked; log: {out.splitlines()}"
    merged = landed_merge(case, pr)
    assert merged is not None and merged["head"] == head
    assert first_parent(case, merged["main"]) == checked


# ---------------------------------------------------------------- statement 2

@pytest.mark.parametrize("which", QUEUED)
def test_queued_writes_apply_after_the_merge_on_the_main_it_produced(proto, monkeypatch, capsys, which):
    case = proto
    install(case, monkeypatch)
    pr, head, checked = checked_single_pr(case, capsys)
    row = queue(case, which)

    support.gate(case)
    merged = landed_merge(case, pr)
    assert merged is not None and first_parent(case, merged["main"]) == checked, (
        "#5 lands first, on the main its checks ran on")
    if writes_left(case, which, row):
        support.gate(case)  # The same or the next owner pass.
    assert writes_left(case, which, row) == [], (
        f"The queued {which} write applies in the same or the next owner pass; still queued: "
        f"{writes_left(case, which, row)}")
    out = capsys.readouterr().out
    assert check_starts(out, 5) == [], f"#5 is not checked again; log: {out.splitlines()}"

    produced = merged["main"]
    if row is not None:
        landed = commits_touching(case, MEMORY, checked)
        assert len(landed) == 1, f"The memory write lands exactly once, got {len(landed)} commits"
        assert landed[0] != produced and is_ancestor(case, produced, landed[0]), (
            "The memory write lands after #5, on the main #5's merge produced")
        assert main_text(case, MEMORY) == MEMORY_TEXT
        assert row not in writes.pending(case.store)
    if which in {"publication", "both"}:
        assert len(case.publication["applied_on"]) == 1, "The publication applies exactly once"
        applied_on = case.publication["applied_on"][0]
        assert is_ancestor(case, produced, applied_on), (
            f"The publication applies on the main #5 produced ({produced[:12]}), not on {applied_on[:12]}")
        assert len(commits_touching(case, PUBLICATION, checked)) == 1
        assert main_text(case, PUBLICATION).startswith(PUBLICATION_TEXT)
    tip = main_tip(case)
    assert is_ancestor(case, produced, tip), "Main keeps #5's merge under the writes"
    assert main_text(case, "src/member_5.py") == "VALUE = 5", "#5's change stays on main"
    assert main_text(case, NEIGHBOR).startswith("OTHER = 'main moved"), "Main's earlier commit stays"
    assert len(case.merges) == 1, "#5 merged exactly once"

    support.gate(case)  # Nothing is left: a later pass writes nothing again.
    assert main_tip(case) == tip, "A later pass does not apply the landed writes again"


# ---------------------------------------------------------------- statement 3

def test_after_a_checked_pr_landed_first_writes_apply_as_today_while_the_next_pr_is_not_ready(
        proto, monkeypatch, capsys):
    """The new order applies only to checked work: with no PR ready, the writes do not wait."""
    case = proto
    install(case, monkeypatch)
    pr, head, checked = checked_single_pr(case, capsys)
    queue(case, "memory")
    support.gate(case)
    merged = landed_merge(case, pr)
    assert merged is not None and merged["head"] == head and first_parent(case, merged["main"]) == checked, (
        "#5 (checked on the current main) lands first")
    support.gate(case)
    assert not writes.pending(case.store)

    # PR #1 joins the queue but its CI has not ended: nothing is ready to land.
    support.queue_workers(case, [1])
    second = support.pr_for(case, 1)
    case.tracker.set_pr_checks(second.number, "pending")
    before = main_tip(case)
    row = queue_memory(case, "Lesson queued while #1 waits")
    queue_publication(case)

    support.gate(case)
    assert memory_status(case, row) == "done", "With no PR ready to land, the memory write applies in this pass"
    assert len(case.publication["applied_on"]) == 1 and is_ancestor(case, before, case.publication["applied_on"][0]), (
        "With no PR ready to land, the publication applies in this pass, once")
    assert landed_merge(case, second) is None, "#1's CI is still pending; it does not merge"
    assert len(commits_touching(case, MEMORY, before)) == 1

    case.tracker.set_pr_checks(second.number, "pass")
    for _ in range(4):
        support.gate(case)
        if landed_merge(case, second):
            break
    assert landed_merge(case, second) is not None, "#1 merges later, on the main that holds the writes"
    assert main_text(case, MEMORY) == "Lesson queued while #1 waits"
    assert main_text(case, "src/member_1.py") == "VALUE = 1" and main_text(case, "src/member_5.py") == "VALUE = 5"
