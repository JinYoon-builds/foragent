"""Closed order parents must finish their cards despite competing parent work."""

from dataclasses import replace
from math import isfinite

from foragent import board, gh
from foragent.tracker import RecordingTracker, TrackerError
from test_board_projects import board_snapshot
from test_board_status_groups import seed_card
from test_orch_review_handoff import tick_context
from test_parent_follows_children import (
    DONE, add_parent, offline_only, parent_case,  # noqa: F401
)
from test_parent_sync_starve import Latency
from test_skills_autosync import isolated_home  # noqa: F401


class CardWriteLatency(Latency):
    """Use #1577's clock and real gate limits, with fast board reads.

    Creating a card and setting Status are two four-second mutations: only
    one new Done card fits in the shared ten-second budget. Fast reads keep
    this a fairness test rather than a test of retaining multi-tick reads.
    Other parent calls, including closing a competing order, still take 4 s.
    """

    def __init__(self, tracker, monkeypatch):
        graphql = tracker.graphql
        super().__init__(tracker, monkeypatch)
        slow_graphql = tracker.graphql

        def fast_reads(query, variables):
            if not query.lstrip().startswith("query"):
                return slow_graphql(query, variables)
            # Even a fast read cannot start after the real gh deadline.
            try:
                available = gh.call_timeout(float("inf"))
            except gh.DeadlineExhausted as exc:
                self.calls.append(("graphql", query, True, 0.0))
                raise TrackerError(str(exc)) from exc
            self.calls.append(("graphql", query, isfinite(available), 0.0))
            return graphql(query, variables)

        monkeypatch.setattr(tracker, "graphql", fast_reads)


def test_every_cardless_closed_parent_gets_a_done_card_within_bounded_ticks(
    parent_case, monkeypatch,
):
    case = parent_case
    case.config = case.ctx.config = replace(case.config, language="en")
    parents = tuple(range(2000, 2006))
    limit = len(parents) + 2
    for number in parents:
        add_parent(case, ["completed"], number=number)
        case.tracker.issues[number] = replace(
            case.tracker.issues[number], state="closed", state_reason="completed",
        )

    # Like an old closed order on the real board, none has a card, and the
    # periodic listing/board read has already recorded that absence.
    project = case.tracker._projects.projects[case.board_id]
    project["items"] = {
        key: item for key, item in project["items"].items()
        if item["contentId"] not in {f"I_owner/repo#{number}" for number in parents}
    }
    context = tick_context(case, start_runs=False)
    recorder = RecordingTracker(case.tracker, context)
    recorder.refresh_issues(observed_at=case.clock[0])
    board.refresh_cards(context, recorder)
    assert all(case.store.local_board_value(number, case.board_id, "Status") == board.NO_CARD
               for number in parents)

    failed = []
    graphql = case.tracker.graphql

    def fail_first_card_write(query, variables):
        if "addProjectV2ItemById(" in query:
            number = int(variables["input"]["contentId"].rsplit("#", 1)[1])
            if number in parents and not failed:
                failed.append(number)
                raise TrackerError("temporary board write failure", returncode=1)
        return graphql(query, variables)

    monkeypatch.setattr(case.tracker, "graphql", fail_first_card_write)
    latency = CardWriteLatency(case.tracker, monkeypatch)
    exhausted_ticks = 0
    done_before = set()
    for turn in range(limit):
        # Fresh ready-to-close work competes on every listing tick. Its card
        # is already Done, but its issue close still costs four seconds. That
        # leaves too little time for a cardless parent's two mutations if
        # closing orders always get the first turn.
        competitor = 10 + turn
        add_parent(case, ["completed"], number=competitor)
        seed_card(case.tracker, case.board_id, competitor, DONE)
        assert latency.gate_tick(case).skipped is None
        bounded = [call for call in latency.calls if call[2]]
        exhausted_ticks += sum(call[3] for call in bounded) == 10.0
        items = board_snapshot(case.tracker, case.board_id)["items"]["nodes"]
        done_now = {
            item["content"]["number"] for item in items
            if item["content"]["number"] in parents
            and any(value["field"]["name"] == "Status" and value["name"] == DONE
                    for value in item["fieldValues"]["nodes"])
        }
        assert len(done_now - done_before) <= 1, "The fake must admit at most one new Done card per tick"
        done_before = done_now

    assert len(failed) == 1, "Exercise one real failed card write, not just deadline deferrals"
    assert exhausted_ticks, "Competing parent work must reach the real shared gh deadline"
    cards = {
        number: [
            next((value["name"] for value in item["fieldValues"]["nodes"]
                  if value["field"]["name"] == "Status"), None)
            for item in items if item["content"]["number"] == number
        ]
        for number in parents
    }
    assert cards == {number: [DONE] for number in parents}, (
        f"After {limit} ticks every closed parent must have exactly one Done card, "
        f"including failed parent #{failed[0]}; got {cards}"
    )
