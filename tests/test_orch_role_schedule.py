"""Order-finish wakes (D99): what the locked acceptance tests leave open.

Launches stop at the fake Popen of the acceptance fixture; no real process starts or is signaled.
"""

from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "acceptance"))

from foragent import executors, orch_architect, orch_dream, orch_inbox, orch_role_schedule as schedule, scheduler
from foragent.executors import ExecResult
from foragent.tracker import Issue
from test_architect_project_schedule import add_record, complete, order_children, review_case  # noqa: F401
from test_board_status_groups import offline_github  # noqa: F401
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_orch_review_handoff import tick_context
from test_skills_autosync import isolated_home  # noqa: F401


def marker(*children):
    mapping = json.dumps({f"child-{child}": child for child in children})
    return f"<!-- foragent:plan id=order map={mapping} acceptance={{}} -->"


def roles(case, *names):
    entries = {name: {"agent": f"fa-{name}", "skills": [f"fa-{name}"], "memory": f".claude/agent-memory/fa-{name}/",
                      "signals": []} for name in names}
    case.ctx.config = replace(case.ctx.config, roles=entries)


def facts(case, role):
    section = schedule.facts(case.store, case.ctx.config, case.clock[0])
    return next(item for item in section["roles"] if item["role"] == role)


def finish(case, role):
    record = orch_architect.read_record(case.store, role)
    assert orch_architect.run_session(case.root, case.store, case.ctx.config, record["why"],
                                      record["started_at"], lambda: case.clock[0], role) == 0
    case.alive.remove(record["pid"])
    case.process_table.exit(record["pid"])


def retrospectives(case):
    return [row for row in orch_inbox.requests(case.store) if row.get("kind") == "retrospective"]


def test_finished_orders_needs_every_known_child_closed_and_skips_ordinary_work():
    closed = Issue(1, "", "", frozenset(), "closed")
    still_open = Issue(2, "", "", frozenset(), "open")
    order = lambda number, *children, labels=(), state="open": Issue(  # noqa: E731
        number, "", marker(*children), frozenset(labels), state)
    assert schedule.finished_orders([closed, order(9, 1)]) == {9: {1}}
    assert schedule.finished_orders([closed, order(9, 1, state="closed")]) == {9: {1}}, "The gate closes finished orders"
    assert schedule.finished_orders([closed, still_open, order(9, 1, 2)]) == {}
    assert schedule.finished_orders([closed, order(9, 1, 3)]) == {}, "An unknown child is not a closed child"
    assert schedule.finished_orders([closed, order(9, 1, labels=["approved"])]) == {}
    assert schedule.finished_orders([closed, order(9, 1, labels=["lane:orch"])]) == {}
    assert schedule.finished_orders([closed, order(9)]) == {}, "An order with no children has not finished"
    assert schedule.finished_orders([closed, order(9, 1, 9)]) == {9: {1}}, "A record may name itself"


def test_a_lane_dream_needs_a_finished_order_with_a_merge_of_that_lane_since_its_last_dream(review_case):
    case = review_case
    config = replace(case.ctx.config, lanes={"orch": {"paths": ["a/**"]}, "view": {"paths": ["b/**"]}})
    case.store.start_run(5, branch="b5", worktree=case.store.worktree_path(5), lane="orch")
    case.store.finish_run(5, "merged")
    case.store.start_run(6, branch="b6", worktree=case.store.worktree_path(6), lane="view")
    case.store.finish_run(6, "merged")
    at = case.clock[0].isoformat()
    issues = [Issue(5, "", "", frozenset(), "closed", updated_at=at), Issue(6, "", "", frozenset(), "open"),
              Issue(9, "", marker(5), frozenset(), "open"), Issue(10, "", marker(5, 6), frozenset(), "open")]
    tracker = SimpleNamespace(list_issues=lambda state="all": issues)
    assert orch_dream.dream_due(case.store, config, tracker, case.clock[0]) == ["orch"], (
        "A merge in view belongs to an unfinished order")


def test_an_order_finishing_after_a_dream_keeps_its_turn_for_merges_that_dream_already_saw(review_case):
    case = review_case
    config = replace(case.ctx.config, lanes={"orch": {"paths": ["a/**"]}})
    for number in (5, 6, 7):  # 5 is order A's child; 6 and 7 are order B's, and 7 stays open past A's dream.
        case.store.start_run(number, branch=f"b{number}", worktree=case.store.worktree_path(number), lane="orch")
        case.store.finish_run(number, "merged" if number != 7 else "blocked")
    early = case.clock[0].isoformat()
    closed = [Issue(5, "", "", frozenset(), "closed", updated_at=early), Issue(6, "", "", frozenset(), "closed", updated_at=early)]
    orders = [Issue(9, "", marker(5), frozenset(), "open"), Issue(10, "", marker(6, 7), frozenset(), "open")]

    def due(seven):
        tracker = SimpleNamespace(list_issues=lambda state="all": [*closed, *orders, seven])
        return orch_dream.dream_due(case.store, config, tracker, case.clock[0])

    assert due(Issue(7, "", "", frozenset(), "open")) == ["orch"], "Order A finished"
    case.clock[0] += timedelta(minutes=1)
    orch_dream.record_done(case.store, "orch", 0, "en", case.clock[0])
    assert due(Issue(7, "", "", frozenset(), "open")) == [], "A's dream answered A; B has not finished"
    case.clock[0] += timedelta(minutes=1)
    assert due(Issue(7, "", "", frozenset(), "closed", updated_at=case.clock[0].isoformat())) == ["orch"], (
        "B finished after the dream, so its earlier merge in the lane still earns B a dream")
    case.clock[0] += timedelta(minutes=1)
    orch_dream.record_done(case.store, "orch", 0, "en", case.clock[0])
    assert due(Issue(7, "", "", frozenset(), "closed", updated_at=early)) == []


def test_a_lane_with_only_failed_work_in_a_finished_order_does_not_dream(review_case):
    case = review_case
    config = replace(case.ctx.config, lanes={"orch": {"paths": ["a/**"]}})
    case.store.start_run(5, branch="b5", worktree=case.store.worktree_path(5), lane="orch")
    case.store.finish_run(5, "blocked")
    issues = [Issue(5, "", "", frozenset(), "closed", updated_at=case.clock[0].isoformat()),
              Issue(9, "", marker(5), frozenset(), "open")]
    tracker = SimpleNamespace(list_issues=lambda state="all": issues)
    assert orch_dream.dream_due(case.store, config, tracker, case.clock[0]) == []


def test_docs_starts_after_the_architect_each_in_its_own_tick(review_case):
    case = review_case
    roles(case, "architect", "docs")
    scheduler.tick(tick_context(case))
    assert [child.argv[child.argv.index("--role") + 1] for child in case.launches] == ["architect"]
    scheduler.tick(tick_context(case))
    assert [child.argv[child.argv.index("--role") + 1] for child in case.launches] == ["architect", "docs"]
    scheduler.tick(tick_context(case))
    assert len(case.launches) == 2, "One session per role"


def test_the_docs_role_needs_no_new_run_input_but_the_architect_does(review_case):
    case = review_case
    roles(case, "architect", "docs")
    scheduler.tick(tick_context(case))
    finish(case, "architect")
    scheduler.tick(tick_context(case))
    finish(case, "docs")
    case.clock[0] += timedelta(minutes=1)
    case.tracker.add_issue(3, "Closed by hand", state="closed")
    order_children(case, 901, [3])
    scheduler.tick(tick_context(case))
    assert case.launches[-1].argv[case.launches[-1].argv.index("--role") + 1] == "docs" and len(case.launches) == 3
    assert facts(case, "architect")["reason"] == "no_new_input"


def test_a_later_run_of_other_work_does_not_wake_the_architect_for_an_order_that_had_no_input(review_case):
    case = review_case
    scheduler.tick(tick_context(case))
    complete(case)
    case.clock[0] += timedelta(minutes=1)
    case.tracker.add_issue(3, "Closed by hand", state="closed")
    order_children(case, 901, [3])
    scheduler.tick(tick_context(case))
    case.clock[0] += timedelta(minutes=1)
    add_record(case, 7)  # Unrelated work, in no finished order.
    scheduler.tick(tick_context(case))
    assert len(case.launches) == 1


def test_a_later_edit_of_a_closed_child_does_not_finish_its_order_again(review_case):
    case = review_case
    roles(case, "docs")
    scheduler.tick(tick_context(case))
    finish(case, "docs")
    case.clock[0] += timedelta(hours=1)
    case.tracker.edit_body(1, case.tracker.get_issue(1).body + "\nA later comment.")
    scheduler.tick(tick_context(case))
    assert len(case.launches) == 1
    events = (case.store.root / "orch-events.jsonl").read_text()
    assert events.count('"kind": "order-finished"') == 1


def test_an_order_past_the_journal_lifetime_is_not_recorded_and_wakes_nothing(review_case):
    case = review_case
    case.clock[0] += schedule.KEEP
    scheduler.tick(tick_context(case))
    journal = case.store.root / "orch-events.jsonl"
    assert case.launches == [] and (not journal.exists() or "order-finished" not in journal.read_text())


@pytest.mark.parametrize("hold", ["usage", "executor", "hours"])
def test_quota_executor_pause_and_run_hours_hold_session_and_request(review_case, hold):
    case = review_case
    roles(case, "architect", "orchestrator")
    if hold == "usage":
        case.store.record_usage(case.ctx.config.executor, [{"used_percent": 100}])
    elif hold == "executor":
        case.store.pause_executor(case.ctx.config.executor, case.clock[0] + timedelta(hours=3))
    else:
        now = case.clock[0].astimezone().hour
        case.ctx.config = replace(case.ctx.config, run_hours=f"{(now + 2) % 24:02d}-{(now + 3) % 24:02d}")
    scheduler.tick(tick_context(case))
    assert case.launches == [] and retrospectives(case) == []
    section = schedule.facts(case.store, case.ctx.config, case.clock[0])
    assert section["suppressed"] == {"usage": "quota", "executor": "executor_pause", "hours": "run_hours"}[hold]
    assert facts(case, "architect")["due"] is True


def test_a_failed_start_retries_after_an_hour_or_a_later_reset_without_stopping_the_tick(review_case, monkeypatch):
    case = review_case
    calls = []

    def fail(*args, **kwargs):
        calls.append(args)
        raise OSError("no such executable")

    monkeypatch.setattr(orch_architect, "call", fail)
    assert scheduler.tick(tick_context(case)).skipped is None and len(calls) == 1
    item = facts(case, "architect")
    assert item["due"] is True and item["last_error"] == "no such executable"
    assert item["next_retry"] == (case.clock[0] + timedelta(hours=1)).isoformat()
    case.clock[0] += timedelta(minutes=59)
    scheduler.tick(tick_context(case))
    assert len(calls) == 1
    reset = case.clock[0] + timedelta(hours=2)
    case.store.pause_executor(case.ctx.config.executor, reset)
    assert facts(case, "architect")["next_retry"] == reset.isoformat(), "A later reset moves the retry"
    case.clock[0] = reset + timedelta(seconds=1)
    scheduler.tick(tick_context(case))
    assert len(calls) == 2


def test_a_capacity_refusal_stays_due_with_no_failure_and_no_wait(review_case, monkeypatch):
    case = review_case
    monkeypatch.setattr(orch_architect, "call", lambda *a, **k: (_ for _ in ()).throw(
        orch_architect.ArchitectRefused("no slot")))
    scheduler.tick(tick_context(case))
    item = facts(case, "architect")
    assert item["due"] is True and item["last_error"] is None and item["next_retry"] is None


def test_a_session_that_died_before_writing_done_waits_the_hour_from_its_call(review_case):
    case = review_case
    scheduler.tick(tick_context(case))
    record = orch_architect.read_record(case.store)
    case.alive.remove(record["pid"])
    case.process_table.exit(record["pid"])
    case.clock[0] += timedelta(minutes=59)
    scheduler.tick(tick_context(case))
    assert len(case.launches) == 1
    case.clock[0] += timedelta(minutes=1)
    scheduler.tick(tick_context(case))
    assert len(case.launches) == 2


def test_a_failed_session_does_not_advance_the_input_cutoff(review_case, monkeypatch):
    case = review_case
    scheduler.tick(tick_context(case))
    record = orch_architect.read_record(case.store)
    monkeypatch.setattr(executors, "executor_for", lambda _: SimpleNamespace(
        run=lambda **kw: ExecResult(7, False, "failed", kw["log_path"])))
    orch_architect.run_session(case.root, case.store, case.ctx.config, record["why"], record["started_at"],
                               lambda: case.clock[0])
    assert facts(case, "architect")["input_cutoff"] is None


def test_a_retrospective_closed_with_no_marked_note_is_asked_again_after_the_hour(review_case):
    case = review_case
    roles(case, "orchestrator")
    scheduler.tick(tick_context(case))
    first, = retrospectives(case)
    orch_inbox.set_request_status(case.store, first["id"], "done", "Saw it")
    case.clock[0] += timedelta(minutes=59)
    scheduler.tick(tick_context(case))
    assert len(retrospectives(case)) == 1, "Closing a request is not completion, and it waits for the floor"
    case.clock[0] += timedelta(minutes=1)
    scheduler.tick(tick_context(case))
    assert len(retrospectives(case)) == 2


def test_one_pending_retrospective_names_every_finished_order_and_a_marked_note_covers_them(review_case):
    from foragent import orch_events

    case = review_case
    roles(case, "orchestrator")
    case.tracker.add_issue(3, "Closed", state="closed")
    order_children(case, 901, [3])
    scheduler.tick(tick_context(case))
    request, = retrospectives(case)
    assert sorted(request["orders"]) == [900, 901] and "#900" in request["text"] and "#901" in request["text"]
    assert facts(case, "retrospective")["pending"] == request["id"]
    case.clock[0] += timedelta(minutes=5)
    assert facts(case, "retrospective")["pending_age_seconds"] == 300
    orch_events.append_event(case.store, case.clock[0], None, "note", "reviewed", now="reviewed", retrospective=True)
    orch_inbox.set_request_status(case.store, request["id"], "done", "Recorded lessons")
    case.clock[0] += timedelta(hours=2)
    scheduler.tick(tick_context(case))
    assert len(retrospectives(case)) == 1
    assert facts(case, "retrospective")["last_success"] is not None


def test_an_order_finishing_while_a_request_waits_is_not_covered_by_the_note_that_answers_the_request(review_case):
    from foragent import orch_events

    case = review_case
    roles(case, "orchestrator")
    scheduler.tick(tick_context(case))
    first, = retrospectives(case)
    assert first["orders"] == [900]
    case.clock[0] += timedelta(minutes=1)
    case.tracker.add_issue(3, "Closed", state="closed")
    order_children(case, 901, [3])
    scheduler.tick(tick_context(case))
    assert len(retrospectives(case)) == 1, "One pending request at a time"
    case.clock[0] += timedelta(minutes=1)
    orch_events.append_event(case.store, case.clock[0], None, "note", "reviewed", now="reviewed", retrospective=True)
    orch_inbox.set_request_status(case.store, first["id"], "done", "Recorded lessons")
    scheduler.tick(tick_context(case))
    rows = retrospectives(case)
    assert len(rows) == 2 and rows[1]["orders"] == [901], "The note answered 900, which the request named; 901 waits"
    case.clock[0] += timedelta(minutes=1)
    orch_events.append_event(case.store, case.clock[0], None, "note", "reviewed", now="reviewed", retrospective=True)
    orch_inbox.set_request_status(case.store, rows[1]["id"], "done", "Recorded lessons")
    case.clock[0] += timedelta(hours=2)
    scheduler.tick(tick_context(case))
    assert len(retrospectives(case)) == 2


def test_a_note_answers_an_open_request_so_a_new_order_does_not_wait_for_it_to_close(review_case):
    from foragent import orch_events

    case = review_case
    roles(case, "orchestrator")
    scheduler.tick(tick_context(case))
    case.clock[0] += timedelta(minutes=1)
    orch_events.append_event(case.store, case.clock[0], None, "note", "reviewed", now="reviewed", retrospective=True)
    case.clock[0] += timedelta(minutes=1)
    case.tracker.add_issue(3, "Closed", state="closed")
    order_children(case, 901, [3])
    scheduler.tick(tick_context(case))
    assert [row["orders"] for row in retrospectives(case)] == [[900], [901]]


def test_only_listed_roles_wake_and_the_rest_show_as_unavailable(review_case):
    case = review_case
    case.ctx.config = replace(case.ctx.config, roles={})
    scheduler.tick(tick_context(case))
    assert case.launches == [] and retrospectives(case) == []
    assert facts(case, "architect")["available"] is False and facts(case, "retrospective")["available"] is False


def test_human_lines_are_localized_and_name_the_hold(review_case):
    case = review_case
    roles(case, "architect")
    case.ctx.config = replace(case.ctx.config, scheduled_roles=False)
    scheduler.tick(tick_context(case))
    section = schedule.facts(case.store, case.ctx.config, case.clock[0])
    assert any("held (switch)" in line for line in schedule.human_lines(section, "en"))
    assert any("보류됨 (switch)" in line for line in schedule.human_lines(section, "ko"))


def test_with_no_opener_the_request_stays_pending_and_a_launch_is_no_completion(review_case, monkeypatch):
    case = review_case
    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    roles(case, "orchestrator")
    scheduler.tick(tick_context(case))
    request, = retrospectives(case)
    case.clock[0] += timedelta(minutes=5)
    scheduler.tick(tick_context(case))
    item = facts(case, "retrospective")
    assert (item["pending"], item["reason"], item["pending_age_seconds"]) == (request["id"], "pending_request", 300)
    assert item["last_success"] is None and item["last_attempt"] is not None
    assert len(retrospectives(case)) == 1
    orch_inbox.write_note(case.store, "Opened a session", None, None, {"kind": "terminal"})
    assert facts(case, "retrospective")["last_success"] is None, "An ordinary note is not completion"
    assert facts(case, "retrospective")["pending"] == request["id"]


def test_the_last_retrospective_outlives_the_trim_and_later_status_notes(review_case):
    case = review_case
    roles(case, "orchestrator")
    orch_inbox.write_note(case.store, "Lessons", None, None, {"kind": "terminal"}, retrospective=True)
    last = facts(case, "retrospective")["last_success"]
    assert last is not None
    case.clock[0] += timedelta(days=40)
    orch_inbox.write_note(case.store, "Watching", None, None, {"kind": "terminal"})
    case.store.prune_logs(case.clock[0])
    assert facts(case, "retrospective")["last_success"] == last


def test_a_refused_retrospective_note_writes_nothing_even_with_person_found(tmp_path, monkeypatch, capsys):
    from test_order_progress import offline_local_cli
    from test_skills_autosync import cli_exit

    case = offline_local_cli(tmp_path, monkeypatch)
    journal = case.store.root / "orch-events.jsonl"
    before = journal.read_bytes() if journal.exists() else b""
    command = ["orch", "note", "--retrospective", "--person-found", "Found it", "--repo-root", str(case.root)]
    assert cli_exit(case.main, command) != 0
    assert (journal.read_bytes() if journal.exists() else b"") == before
    assert cli_exit(case.main, [*command, "--now", "Lessons"]) == 0
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [row["kind"] for row in rows if row["kind"] in ("person-found", "note")] == ["person-found", "note"]
    assert next(row for row in rows if row["kind"] == "note")["retrospective"] is True
    output = capsys.readouterr().out
    assert "Person-found event recorded" in output and "Retrospective note saved" in output
