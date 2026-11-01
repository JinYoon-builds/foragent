"""#837 with the real StateStore and RecordingTracker across listing refreshes."""

import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))

from test_order_record_refs import add_record, add_work, tick_again
from test_parent_follows_children import (
    BACKLOG, IN_PROGRESS, add_parent, card_column, gate_tick, offline_only, parent_case, seed_card,  # noqa: F401
)
from test_skills_autosync import isolated_home  # noqa: F401

import pytest

from foragent.scheduler import tick
from foragent.tracker import TrackerError
from test_orch_review_handoff import tick_context


def test_unlisted_reference_read_closed_earlier_is_read_again_before_closing(parent_case):  # noqa: F811
    case = parent_case
    add_work(case, 5, closed=True)
    add_work(case, 6)
    add_record(case, 40, edited=[5, 6])
    # Both are outside the listing, so the tick reads them: #5 closed, #6 open.
    listing, recent = case.tracker.list_issues, case.tracker.issues_closed_since
    case.tracker.list_issues = lambda *a, **k: [i for i in listing(*a, **k) if i.number not in {5, 6}]
    case.tracker.issues_closed_since = lambda *a, **k: [i for i in recent(*a, **k) if i.number not in {5, 6}]
    real, unreadable, reads = case.tracker.get_issue, [], []

    def get_issue(n):
        reads.append(n)
        if n in unreadable:
            raise TrackerError(f"Could not resolve to an issue with the number of {n}. (Not Found)")
        return real(n)

    case.tracker.get_issue = get_issue
    gate_tick(case)
    assert case.tracker.get_issue(40).state == "open"
    assert {5, 6} <= set(reads), "The tick read #5 closed and #6 open"
    unreadable.append(5)
    case.tracker.close_issue(6)
    tick_again(case)
    assert case.tracker.get_issue(40).state == "open", "A reference that cannot be read keeps the record open"
    unreadable.clear()
    tick_again(case)
    assert case.tracker.get_issue(40).state == "closed"
    assert [entry for entry in case.tracker.closures if entry[0] == 40] == [(40, "completed")]


def test_record_with_many_unlisted_closed_references_closes_in_one_tick(parent_case):  # noqa: F811
    case = parent_case
    edited = list(range(100, 125))
    for number in edited:
        add_work(case, number, closed=True)
    add_record(case, 40, edited=edited)
    hidden = set(edited)
    listing, recent = case.tracker.list_issues, case.tracker.issues_closed_since
    case.tracker.list_issues = lambda *a, **k: [i for i in listing(*a, **k) if i.number not in hidden]
    case.tracker.issues_closed_since = lambda *a, **k: [i for i in recent(*a, **k) if i.number not in hidden]
    real, reads = case.tracker.get_issue, []

    def get_issue(n):
        if n in hidden:
            reads.append(n)
        return real(n)

    case.tracker.get_issue = get_issue
    gate_tick(case)
    assert case.tracker.get_issue(40).state == "closed", "Every unlisted reference is read in the same tick"
    assert sorted(reads) == edited, "Each unlisted reference is read exactly once"


def _slow(case, monkeypatch):
    import test_parent_sync_starve as starve

    monkeypatch.setattr(starve, "PER_ISSUE", (*starve.PER_ISSUE, "issues_closed_since", "find_pr"))
    return starve.Latency(case.tracker, monkeypatch)


def _hide(case, monkeypatch, hidden):
    listing, recent = case.tracker.list_issues, case.tracker.issues_closed_since
    monkeypatch.setattr(case.tracker, "list_issues", lambda *a, **k: [
        i for i in listing(*a, **k) if i.number not in hidden])
    monkeypatch.setattr(case.tracker, "issues_closed_since", lambda *a, **k: [
        i for i in recent(*a, **k) if i.number not in hidden])


def _reads(latency, name="get_issue"):
    return [call[1] for call in latency.calls if call[0] == name and call[3] == 4.0]


def test_interrupted_action_continues_after_its_last_completed_read(parent_case, monkeypatch):  # noqa: F811
    case = parent_case
    edited = [100, 101, 102, 103, 104]
    for number in edited:
        add_work(case, number, closed=True)
    add_record(case, 40, edited=edited)
    _hide(case, monkeypatch, set(edited))
    latency = _slow(case, monkeypatch)
    read = []
    for _ in range(8):
        latency.calls.clear()
        case.clock[0] += timedelta(minutes=5, seconds=1)
        tick(tick_context(case, start_runs=False))
        read += _reads(latency)
        if case.tracker.issues[40].state == "closed":
            break
    assert case.tracker.issues[40].state == "closed"
    assert read == edited, "Each read happens once, across ticks"
    assert len(case.tracker.comments[40]) == 1


def test_action_that_ran_to_its_end_reads_unlisted_references_fresh(parent_case, monkeypatch):  # noqa: F811
    """Only the listing can invalidate a memory, and it cannot show an unlisted issue closing."""
    case = parent_case
    add_work(case, 100)
    add_record(case, 40, edited=[100])
    _hide(case, monkeypatch, {100})
    latency = _slow(case, monkeypatch)
    for _ in range(2):
        latency.calls.clear()
        case.clock[0] += timedelta(minutes=5, seconds=1)
        tick(tick_context(case, start_runs=False))
        assert _reads(latency) == [100]
    case.tracker.issues[100] = replace(case.tracker.issues[100], state="closed", state_reason="completed")
    for _ in range(2):  # The fresh read, then the close, whatever else shares the budget.
        case.clock[0] += timedelta(minutes=5, seconds=1)
        tick(tick_context(case, start_runs=False))
    assert case.tracker.issues[40].state == "closed", "The close follows the fresh read"


def test_changed_listing_ends_an_interrupted_actions_reads(parent_case, monkeypatch):  # noqa: F811
    case = parent_case
    edited = [100, 101, 102]
    for number in edited:
        add_work(case, number, closed=True)
    add_record(case, 40, edited=edited)
    _hide(case, monkeypatch, set(edited))
    latency = _slow(case, monkeypatch)
    case.clock[0] += timedelta(minutes=5, seconds=1)
    tick(tick_context(case, start_runs=False))
    assert case.tracker.issues[40].state == "open"
    case.tracker.issues[100] = replace(case.tracker.issues[100], state="open", state_reason=None)
    case.tracker.issues[40] = replace(case.tracker.issues[40], updated_at=case.clock[0].isoformat())
    for _ in range(4):
        case.clock[0] += timedelta(minutes=5, seconds=1)
        tick(tick_context(case, start_runs=False))
    assert case.tracker.issues[40].state == "open", "The old closed read must not close over open work"


def test_card_added_then_moved_across_ticks_with_one_card(parent_case, monkeypatch):  # noqa: F811
    """Adding a card and writing its column takes more calls than one tick fits, with one card at the end."""
    from test_parent_follows_children import add_parent

    case = parent_case
    children = add_parent(case, ["unapproved", "unapproved"])
    gate_tick(case)
    gate_tick(case, refresh=True)
    project = case.tracker._projects.projects[case.board_id]
    project["items"] = {key: item for key, item in project["items"].items() if item["contentId"] != "I_owner/repo#10"}
    case.tracker.close_issue(children[0])
    latency = _slow(case, monkeypatch)
    calls = []
    for _ in range(6):
        latency.calls.clear()
        case.clock[0] += timedelta(minutes=5, seconds=1)
        tick(tick_context(case, start_runs=False))
        calls.append([call[0] for call in latency.calls if call[2] and call[3] == 4.0])
        items = [item for item in project["items"].values() if item["contentId"] == "I_owner/repo#10"]
        if items and items[0]["values"]:
            break
    assert len(items) == 1 and card_column(case.tracker, case.board_id, 10) == IN_PROGRESS, calls
    assert len(calls) <= 4, f"Each tick completes one more of the four calls: {calls}"


def test_remembered_child_read_stops_repeating_and_never_hides_open_work(parent_case, monkeypatch):  # noqa: F811
    """The summary keeps counting a child the sub-issue read never returns."""
    case = parent_case
    add_work(case, 5, closed=True)
    add_record(case, 40, mapping={"a": 5})
    case.tracker.add_sub_issue(40, 5)
    listing = case.tracker.list_issues
    monkeypatch.setattr(case.tracker, "list_issues", lambda *a, **k: [
        replace(i, updated_at="2026-01-01T00:00:00+00:00",
                sub_issues_summary=(("total", 2), ("completed", 1), ("percent_completed", 50)))
        if i.number == 40 else i for i in listing(*a, **k)])
    latency = _slow(case, monkeypatch)
    seen = []
    for _ in range(4):
        latency.calls.clear()
        case.clock[0] += timedelta(minutes=5, seconds=1)
        tick(tick_context(case, start_runs=False))
        seen.append(_reads(latency, "sub_issues"))
    assert case.tracker.issues[40].state == "open", "The summary counts open work"
    assert sum(map(len, seen[1:])) <= 1 and seen[-1] == [], f"The read is not repeated: {seen}"


def test_changed_child_row_ends_a_remembered_child_read(parent_case, monkeypatch):  # noqa: F811
    """The parent's own row stays the same; the child's listed row is what changed."""
    case = parent_case
    add_work(case, 5, closed=True)
    add_record(case, 40, mapping={"a": 5})
    case.tracker.add_sub_issue(40, 5)
    listing = case.tracker.list_issues
    monkeypatch.setattr(case.tracker, "list_issues", lambda *a, **k: [
        replace(i, updated_at="2026-01-01T00:00:00+00:00",
                sub_issues_summary=(("total", 3), ("completed", 3), ("percent_completed", 100)))
        if i.number == 40 else i for i in listing(*a, **k)])
    close = case.tracker.close_issue

    def failing(*a, **k):
        raise TrackerError("gh call failed")

    monkeypatch.setattr(case.tracker, "close_issue", failing)
    gate_tick(case, refresh=True)  # Reads the children, remembers them, and the close fails.
    gate_tick(case, refresh=True)
    assert case.tracker.issues[40].state == "open"
    monkeypatch.setattr(case.tracker, "close_issue", close)
    case.tracker.issues[5] = replace(case.tracker.issues[5], state="open", state_reason=None)
    gate_tick(case, refresh=True)
    assert case.tracker.issues[40].state == "open", "The old closed child must not close the parent"


def _children_outside_the_listing(case, monkeypatch, close_fails=0):
    """The total disagrees with the edges, so only a read shows these children."""
    listing = case.tracker.list_issues
    hidden = {5, 6}
    monkeypatch.setattr(case.tracker, "list_issues", lambda *a, **k: [
        replace(i, updated_at="2026-01-01T00:00:00+00:00",
                sub_issues_summary=(("total", 3), ("completed", 3), ("percent_completed", 100)))
        if i.number == 40 else i for i in listing(*a, **k) if i.number not in hidden])
    recent = case.tracker.issues_closed_since
    monkeypatch.setattr(case.tracker, "issues_closed_since", lambda *a, **k: [
        i for i in recent(*a, **k) if i.number not in hidden])
    reads, edges = [], case.tracker.sub_issues

    def counted(number):
        if number == 40:
            reads.append(number)
        return edges(number)

    monkeypatch.setattr(case.tracker, "sub_issues", counted)
    return reads


def test_remembered_open_child_keeps_the_parent_open_while_the_card_catches_up(parent_case, monkeypatch):  # noqa: F811
    case = parent_case
    add_work(case, 5, closed=True)
    add_work(case, 6)
    add_record(case, 40, mapping={"a": 5, "b": 6})
    case.tracker.add_sub_issue(40, 5)
    case.tracker.add_sub_issue(40, 6)
    case.config = case.ctx.config = replace(case.config, language="en")
    reads = _children_outside_the_listing(case, monkeypatch)
    graphql, failing = case.tracker.graphql, [True]

    def fails_once(query, variables):
        if "ForagentBoardIssue" in query and variables.get("number") == 40 and failing:
            failing.clear()
            raise TrackerError("gh call failed")
        return graphql(query, variables)

    monkeypatch.setattr(case.tracker, "graphql", fails_once)
    for _ in range(4):
        gate_tick(case, refresh=True)
    assert case.tracker.issues[40].state == "open", "The remembered child is still open"
    assert card_column(case.tracker, case.board_id, 40) == IN_PROGRESS
    assert len(reads) <= 2, f"The edge read and one action read, then a listing confirms it: {reads}"


def test_remembered_cancelled_child_keeps_its_group_in_the_closing_comment(parent_case, monkeypatch):  # noqa: F811
    case = parent_case
    add_work(case, 5, closed=True)
    add_work(case, 6, closed=True)
    case.tracker.issues[6] = replace(case.tracker.issues[6], state_reason="not_planned")
    add_record(case, 40, mapping={"a": 5, "b": 6})
    case.tracker.add_sub_issue(40, 5)
    case.tracker.add_sub_issue(40, 6)
    case.config = case.ctx.config = replace(case.config, language="en")
    reads = _children_outside_the_listing(case, monkeypatch)
    close, failures = case.tracker.close_issue, [1, 2]

    def fails_twice(number, comment=None):
        if failures:
            failures.pop()
            raise TrackerError("gh call failed")
        return close(number, comment)

    monkeypatch.setattr(case.tracker, "close_issue", fails_twice)
    for _ in range(4):
        gate_tick(case, refresh=True)
    assert case.tracker.issues[40].state == "closed"
    body = case.tracker.comments[40][0].body
    assert body.index("#5") < body.index("Cancelled") < body.index("#6"), body
    assert len(case.tracker.comments[40]) == 1


def test_a_record_read_on_every_tick_cannot_starve_a_later_one(parent_case, monkeypatch):  # noqa: F811
    """Lower records reread an open unlisted reference forever, and only one call fits a tick."""
    case = parent_case
    hidden = set()
    for number in range(40, 46):
        add_work(case, number * 10, closed=number == 45)
        add_record(case, number, edited=[number * 10])
        hidden.add(number * 10)
    _hide(case, monkeypatch, hidden)
    latency = _slow(case, monkeypatch)
    for _ in range(40):
        latency.calls.clear()
        case.clock[0] += timedelta(minutes=5, seconds=1)
        tick(tick_context(case, start_runs=False))
        if case.tracker.issues[45].state == "closed":
            break
    assert case.tracker.issues[45].state == "closed", "The highest record is reached in finite ticks"
    assert all(case.tracker.issues[n].state == "open" for n in range(40, 45))



def _recurring_batch_reads(case):
    """A finished batch child whose open batch PR makes the batch step read GitHub on every tick."""
    case.tracker.add_issue(60, "Batch work", labels=["approved"])
    case.tracker.record_label(60, "approved", "human")
    case.tracker.add_issue(61, "Batch child", state="closed", state_reason="completed")
    child = case.tracker.create_pr(head="foragent/issue-61", base="foragent/batch-60", title="c", body="b")
    case.tracker.merge_pr(child, expected_head_sha=case.tracker.pr_head_sha(child))
    case.tracker.create_pr(head="foragent/batch-60", base="main", title="b", body="b")


def test_recurring_batch_reads_cannot_starve_a_parent_action(parent_case, monkeypatch):  # noqa: F811
    """The closed read and the batch PR reads repeat every tick; the parent must still get its calls."""
    case = parent_case
    _recurring_batch_reads(case)
    gate_tick(case)  # Establish the watermark and the merged PR rows before the record exists.
    edited = [100, 101, 102]
    for number in edited:
        add_work(case, number, closed=True)
    add_record(case, 40, edited=edited)
    _hide(case, monkeypatch, set(edited))
    latency = _slow(case, monkeypatch)
    spent = []
    for _ in range(8):
        latency.calls.clear()
        case.clock[0] += timedelta(minutes=5, seconds=1)
        tick(tick_context(case, start_runs=False))
        spent.append([call[0] for call in latency.calls if call[2] and call[3] == 4.0])
        if case.tracker.issues[40].state == "closed":
            break
    assert case.tracker.issues[40].state == "closed", f"The parent never got a call: {spent}"
    assert len(case.tracker.comments[40]) == 1


def test_batch_step_gets_the_budget_again_once_parents_are_done(parent_case, monkeypatch):  # noqa: F811
    case = parent_case
    _recurring_batch_reads(case)
    gate_tick(case)
    for number in (10, 20, 30):
        add_parent(case, ["completed"], number=number)
    latency = _slow(case, monkeypatch)
    spent = []
    for _ in range(8):
        latency.calls.clear()
        case.clock[0] += timedelta(minutes=5, seconds=1)
        tick(tick_context(case, start_runs=False))
        spent.append([call[0] for call in latency.calls if call[2] and call[3] == 4.0])
    assert all(case.tracker.issues[number].state == "closed" for number in (10, 20, 30))
    assert "issues_closed_since" in spent[-1] and "find_pr" in spent[-1], f"The batch step runs again: {spent}"


def test_a_record_waiting_on_an_unlisted_reference_leaves_the_batch_step_running(parent_case, monkeypatch):  # noqa: F811
    """Its fresh read is the one parent call that repeats every tick."""
    case = parent_case
    _recurring_batch_reads(case)
    gate_tick(case)
    add_work(case, 100)
    add_record(case, 40, edited=[100])
    _hide(case, monkeypatch, {100})
    latency = _slow(case, monkeypatch)
    for _ in range(3):
        latency.calls.clear()
        case.clock[0] += timedelta(minutes=5, seconds=1)
        tick(tick_context(case, start_runs=False))
        names = [call[0] for call in latency.calls if call[2] and call[3] == 4.0]
        assert names[:2] == ["get_issue", "issues_closed_since"], names
    assert case.tracker.issues[40].state == "open"


def test_parent_step_runs_only_on_ticks_that_read_the_listing(parent_case, monkeypatch):  # noqa: F811
    from foragent import board_parents

    case = parent_case
    runs, follow = [], board_parents.follow_children
    monkeypatch.setattr(board_parents, "follow_children", lambda ctx, report: (runs.append(1), follow(ctx, report))[1])
    case.clock[0] += timedelta(minutes=5, seconds=1)
    tick(tick_context(case, start_runs=False))
    assert runs == [1]
    tick(tick_context(case, start_runs=False))  # The listing is still fresh: nothing new to act on.
    assert runs == [1]


def _time_out_status_write(case, monkeypatch, number, times=1):
    """Fail the column write the way an exhausted budget does, leaving everything before it done."""
    graphql, left = case.tracker.graphql, [times]

    def timed_out(query, variables):
        if "ForagentSetBoardValue" in query and left[0]:
            left[0] -= 1
            raise TrackerError("timed out at its deadline")
        return graphql(query, variables)

    monkeypatch.setattr(case.tracker, "graphql", timed_out)


def _delete_card(case, number):
    project = case.tracker._projects.projects[case.board_id]
    project["items"] = {key: item for key, item in project["items"].items()
                        if item["contentId"] != f"I_owner/repo#{number}"}
    return project


def test_child_found_by_a_read_follows_its_listed_row_while_the_action_is_interrupted(parent_case, monkeypatch):  # noqa: F811
    """Stored edges miss the second child; only the read names it, and then its row changes."""
    from test_parent_follows_children import YOUR_TURN

    case = parent_case
    add_work(case, 5, closed=True)
    add_work(case, 6)
    add_record(case, 40, mapping={"a": 5})  # The plan never names #6: only the read does.
    case.tracker.add_sub_issue(40, 5)
    case.tracker.add_sub_issue(40, 6)
    listing, edges, first = case.tracker.list_issues, case.tracker.sub_issues, [True]
    monkeypatch.setattr(case.tracker, "list_issues", lambda *a, **k: [
        replace(i, updated_at="2026-01-01T00:00:00+00:00",
                sub_issues_summary=(("total", 2), ("completed", 1), ("percent_completed", 50)))
        if i.number == 40 else i for i in listing(*a, **k)])

    def racing(number):
        read = edges(number)
        if number == 40 and first:
            first.clear()
            return read[:1]  # The refresh stores one edge though the listing counts two.
        return read

    monkeypatch.setattr(case.tracker, "sub_issues", racing)
    _time_out_status_write(case, monkeypatch, 40)
    gate_tick(case, refresh=True)
    assert next(r for r in case.store.local_issues() if r["number"] == 40)["children"] == [5]
    case.tracker.add_labels(6, ["needs-human"])
    for _ in range(3):
        gate_tick(case, refresh=True)
    assert card_column(case.tracker, case.board_id, 40) == YOUR_TURN, "The child's own row decides its state"


def test_card_deleted_after_its_creation_is_created_again_once(parent_case, monkeypatch):  # noqa: F811
    case = parent_case
    children = add_parent(case, ["unapproved", "unapproved"])
    gate_tick(case)
    gate_tick(case, refresh=True)
    _delete_card(case, 10)
    case.store.record_board_value(10, case.board_id, "Status", None)
    case.tracker.close_issue(children[0])
    _time_out_status_write(case, monkeypatch, 10)
    gate_tick(case, refresh=True)  # Creates the card; the column write runs out of time.
    project = case.tracker._projects.projects[case.board_id]
    assert any(item["contentId"] == "I_owner/repo#10" for item in project["items"].values())
    project = _delete_card(case, 10)
    for _ in range(3):
        gate_tick(case, refresh=True)
    assert card_column(case.tracker, case.board_id, 10) == IN_PROGRESS, "The old card id must not be retried"
    assert sum(item["contentId"] == "I_owner/repo#10" for item in project["items"].values()) == 1


def test_card_deleted_after_its_read_is_found_missing_when_the_write_is_rejected(parent_case, monkeypatch):  # noqa: F811
    case = parent_case
    children = add_parent(case, ["unapproved", "unapproved"])
    gate_tick(case)
    gate_tick(case, refresh=True)
    case.tracker.close_issue(children[0])
    _time_out_status_write(case, monkeypatch, 10)
    gate_tick(case, refresh=True)  # The read saw the card; the write ran out of time.
    project = _delete_card(case, 10)
    for _ in range(3):
        gate_tick(case, refresh=True)
    assert card_column(case.tracker, case.board_id, 10) == IN_PROGRESS
    assert sum(item["contentId"] == "I_owner/repo#10" for item in project["items"].values()) == 1


def test_timed_out_write_keeps_the_board_reads_and_a_rejected_one_forgets_them(parent_case, monkeypatch):  # noqa: F811
    case = parent_case
    children = add_parent(case, ["unapproved", "unapproved"])
    gate_tick(case)
    gate_tick(case, refresh=True)
    case.tracker.close_issue(children[0])
    graphql, reads, failure = case.tracker.graphql, [], [TrackerError("timed out at its deadline")]

    def counted(query, variables):
        if query.lstrip().startswith("query ForagentBoardIssue") and variables.get("number") == 10:
            reads.append(1)
        if "ForagentSetBoardValue" in query and failure[0] is not None:
            raise failure[0]
        return graphql(query, variables)

    monkeypatch.setattr(case.tracker, "graphql", counted)
    gate_tick(case, refresh=True)
    gate_tick(case, refresh=True)
    assert len(reads) == 1, "A write that ran out of time continues without reading again"
    failure[0] = TrackerError("gh: Could not resolve to a node", returncode=1)
    gate_tick(case, refresh=True)
    assert len(reads) == 1, "This tick still used the read; the rejection ends it"
    failure[0] = None
    gate_tick(case, refresh=True)
    assert len(reads) == 2, "After a rejection the next tick reads the board again"
    assert card_column(case.tracker, case.board_id, 10) == IN_PROGRESS


@pytest.mark.parametrize(("failure", "reads_after"), [
    pytest.param(TrackerError("HTTP 429: API rate limit exceeded", returncode=1), 1, id="rate-limit-keeps-reads"),
    pytest.param(TrackerError("GraphQL errors: [{'type': 'NOT_FOUND'}]"), 2, id="graphql-errors-forget-reads"),
])
def test_only_a_rejection_of_the_write_ends_the_board_reads(parent_case, monkeypatch, failure, reads_after):  # noqa: F811
    case = parent_case
    children = add_parent(case, ["unapproved", "unapproved"])
    gate_tick(case)
    gate_tick(case, refresh=True)
    case.tracker.close_issue(children[0])
    graphql, reads, failing = case.tracker.graphql, [], [True]

    def counted(query, variables):
        if query.lstrip().startswith("query ForagentBoardIssue") and variables.get("number") == 10:
            reads.append(1)
        if "ForagentSetBoardValue" in query and failing:
            failing.clear()
            raise failure
        return graphql(query, variables)

    monkeypatch.setattr(case.tracker, "graphql", counted)
    case.clock[0] += timedelta(minutes=5, seconds=1)
    tick(tick_context(case, start_runs=False))
    case.clock[0] = (case.store.github_wait_until(case.clock[0]) or case.clock[0]) + timedelta(minutes=5, seconds=1)
    tick(tick_context(case, start_runs=False))
    assert len(reads) == reads_after
    assert card_column(case.tracker, case.board_id, 10) == IN_PROGRESS


def test_response_carrying_errors_is_not_remembered_as_a_completed_read(parent_case, monkeypatch):  # noqa: F811
    case = parent_case
    children = add_parent(case, ["unapproved", "unapproved"])
    gate_tick(case)
    gate_tick(case, refresh=True)
    case.tracker.close_issue(children[0])
    graphql, failing = case.tracker.graphql, [True]

    def carrying(query, variables):
        if query.lstrip().startswith("query ForagentBoardIssue") and variables.get("number") == 10 and failing:
            failing.clear()
            return {"data": None, "errors": [{"message": "try again"}]}
        return graphql(query, variables)

    monkeypatch.setattr(case.tracker, "graphql", carrying)
    gate_tick(case, refresh=True)
    gate_tick(case, refresh=True)
    assert card_column(case.tracker, case.board_id, 10) == IN_PROGRESS



def _child_only_a_read_names(case, monkeypatch):
    """Stored edges keep #5 only; the plan never names #6, so a read is the only way to learn of it."""
    add_work(case, 5, closed=True)
    add_work(case, 6)
    add_record(case, 40, mapping={"a": 5})
    case.tracker.add_sub_issue(40, 5)
    case.tracker.add_sub_issue(40, 6)
    listing, edges, first = case.tracker.list_issues, case.tracker.sub_issues, [True]
    monkeypatch.setattr(case.tracker, "list_issues", lambda *a, **k: [
        replace(i, updated_at="2026-01-01T00:00:00+00:00",
                sub_issues_summary=(("total", 2), ("completed", 1), ("percent_completed", 50)))
        if i.number == 40 else i for i in listing(*a, **k)])

    def racing(number):
        read = edges(number)
        if number == 40 and first:
            first.clear()
            return read[:1]
        return read

    monkeypatch.setattr(case.tracker, "sub_issues", racing)


def test_listed_row_beats_a_lagging_read_of_the_same_child(parent_case, monkeypatch):  # noqa: F811
    from test_parent_follows_children import YOUR_TURN

    case = parent_case
    _child_only_a_read_names(case, monkeypatch)
    edges = case.tracker.sub_issues
    gate_tick(case, refresh=True)
    case.tracker.add_labels(6, ["needs-human"])
    lagging = replace(case.tracker.issues[6], labels=frozenset())
    monkeypatch.setattr(case.tracker, "sub_issues", lambda number: [
        lagging if issue.number == 6 else issue for issue in edges(number)])
    gate_tick(case, refresh=True)
    gate_tick(case, refresh=True)
    assert card_column(case.tracker, case.board_id, 40) == YOUR_TURN


def test_change_in_a_child_only_a_read_names_ends_the_board_reads(parent_case, monkeypatch):  # noqa: F811
    case = parent_case
    _child_only_a_read_names(case, monkeypatch)
    graphql, reads, failing = case.tracker.graphql, [], [True]

    def counted(query, variables):
        if query.lstrip().startswith("query ForagentBoardIssue") and variables.get("number") == 40:
            reads.append(1)
        if "ForagentSetBoardValue" in query and failing:
            raise TrackerError("timed out at its deadline")
        return graphql(query, variables)

    monkeypatch.setattr(case.tracker, "graphql", counted)
    gate_tick(case, refresh=True)
    gate_tick(case, refresh=True)
    assert len(reads) == 1, "Nothing changed: the read stands"
    case.tracker.add_labels(6, ["blocked"])
    gate_tick(case, refresh=True)
    assert len(reads) == 2, "The listed row it depends on changed"


def test_write_response_carrying_errors_ends_the_board_reads(parent_case, monkeypatch):  # noqa: F811
    case = parent_case
    children = add_parent(case, ["unapproved", "unapproved"])
    gate_tick(case)
    gate_tick(case, refresh=True)
    case.tracker.close_issue(children[0])
    graphql, reads, failing = case.tracker.graphql, [], [True]

    def carrying(query, variables):
        if query.lstrip().startswith("query ForagentBoardIssue") and variables.get("number") == 10:
            reads.append(1)
        if "ForagentSetBoardValue" in query and failing:
            failing.clear()
            return {"data": None, "errors": [{"message": "Could not resolve to a node"}]}
        return graphql(query, variables)

    monkeypatch.setattr(case.tracker, "graphql", carrying)
    gate_tick(case, refresh=True)
    gate_tick(case, refresh=True)
    assert len(reads) == 2
    assert card_column(case.tracker, case.board_id, 10) == IN_PROGRESS


def test_a_write_is_sent_again_when_the_card_needs_it_again(parent_case, monkeypatch):  # noqa: F811
    """The listing cannot show a person moving the card back; a remembered write would hide it."""
    case = parent_case
    children = add_parent(case, ["unapproved", "unapproved"])
    gate_tick(case)
    gate_tick(case, refresh=True)
    case.tracker.close_issue(children[0])
    gate_tick(case, refresh=True)
    assert card_column(case.tracker, case.board_id, 10) == IN_PROGRESS
    seed_card(case.tracker, case.board_id, 10, BACKLOG)
    case.store.record_board_value(10, case.board_id, "Status", BACKLOG)
    gate_tick(case, refresh=True)
    assert card_column(case.tracker, case.board_id, 10) == IN_PROGRESS
