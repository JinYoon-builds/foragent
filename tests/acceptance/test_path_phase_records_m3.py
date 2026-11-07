"""Acceptance tests for path-phase-records, M3 (PM q62 B): no silent end of a pass, no silent lost record.

Statements:
* When the merge owner or the publication pass ends with work still queued, it writes a
  record that names the cause.
* When writing a flow or phase record fails, a visible event names the failure; the record
  is not silently dropped.

Contract chosen by the lane (see the notes of this preparation):

* A "record" is a row the store keeps: an ``orch-events.jsonl`` journal event or a flow
  record (``StateStore.flow_records()``; its ``data`` fields count as the record's fields).
* A queued-work record names the queued work (its issue number in ``issue`` or ``issues``,
  or a publication request id or the order id in any field) and has a non-empty ``cause``
  or ``reason`` text. The tests match words in that text, not exact strings.
* A visible failure event is a journal event, a flow record, or a log or stderr line whose
  text names the record kind ("flow" or "phase") and a failure ("fail", "error", "could not"
  or "unable").

Real merge owner, batch train, gate tick and state from the merge-owner prototype world;
real publication queue and owner pass from the returned-publication world; a real state
store with a fixed clock for the write failures. No network, model or signal.
"""

from collections import Counter
from datetime import timedelta
import json
import logging
import os
import re

import pytest

from helpers.merge_world import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support
from test_merge_owner_batch import batched, groups, queue_workers  # noqa: F401
from test_order_publications import case, submit  # noqa: F401  (fixture)
from test_returned_publication_owner import owner, owner_passes, publication_ids, statuses, strings  # noqa: F401
from test_scorecard_flow import NOW, flow  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401

from foragent import orch_events


FAILURE_WORDS = ("fail", "error", "could not", "unable")
TRAIN_CAUSE = re.compile(r"train|batch|\bci\b")


# ---------------------------------------------------------------- record helpers

def kept_records(store):
    """Every journal event and flow record the store keeps, as flat dicts."""
    try:
        journal = [dict(row) for row in orch_events.read_events(store)]
    except OSError:
        journal = []
    flows = [{**(row.get("data") or {}), "kind": row.get("kind"), "issue": row.get("issue"), "at": row.get("at")}
             for row in store.flow_records()]
    return journal + flows


def _key(record):
    return json.dumps(record, sort_keys=True, default=str)


def written_since(store, before):
    """Records written after the snapshot ``before`` (a Counter of record keys)."""
    after = kept_records(store)
    left = Counter(before)
    new = []
    for record in after:
        key = _key(record)
        if left[key]:
            left[key] -= 1
        else:
            new.append(record)
    return new


def snapshot(store):
    return Counter(_key(record) for record in kept_records(store))


def cause_of(record):
    for field in ("cause", "reason"):
        value = record.get(field)
        if isinstance(value, str) and value.strip():
            return value
    return None


def names_issue(record, n):
    issues = record.get("issues")
    return record.get("issue") == n or (isinstance(issues, list) and n in issues)


def cause_records(records, names):
    return [record for record in records if cause_of(record) and names(record)]


# ---------------------------------------------------------------- merge owner ends with queued work

def test_merge_owner_pass_that_ends_with_issues_still_queued_records_the_cause(batched):
    case = batched
    case.pending_batch = True
    queue_workers(case, [5, 1])
    support.gate(case)
    assert len(groups(case)) == 1, "Fixture: train A of #5 and #1 runs; its CI stays pending"
    queue_workers(case, [4, 2])
    for _ in range(3):
        before = snapshot(case.store)
        case.clock[0] += timedelta(minutes=5)
        support.gate(case)  # One owner pass: #4 and #2 stay queued behind the running train.
        assert len(groups(case)) == 1 and not case.store.owner_merge_facts(), (
            "Fixture: the train's CI is still pending, so #4 and #2 are still queued")
        new = written_since(case.store, before)
        for n in (4, 2):
            named = cause_records(new, lambda record, n=n: names_issue(record, n))
            found = [record for record in named if TRAIN_CAUSE.search(cause_of(record).lower())]
            assert found, (
                f"The owner pass ended with #{n} still queued behind the running train's pending CI, but no "
                f"record written in this pass names #{n} with that cause. Records naming #{n} with a cause or "
                f"reason: {named}. Records written in this pass: {new[:12]}")
            assert len(found) == 1, f"One pass records #{n}'s cause once, not {len(found)} times: {found}"
        assert not cause_records(new, lambda record: names_issue(record, 3)), (
            "Only queued work gets a queued-work cause record")


# ---------------------------------------------------------------- publication pass ends with queued work

def test_publication_pass_that_ends_with_a_publication_still_queued_records_the_cause(owner):
    case = owner
    submit(case)
    ids = publication_ids(case)
    assert ids, "Fixture: the order is a pending publication request"
    case.store.pause_github(case.clock[0] + timedelta(minutes=30), "GitHub returned 502 Bad Gateway")
    before = snapshot(case.store)
    owner_passes(case, 1)
    assert statuses(case) and statuses(case)[-1] not in {"published", "preparation"}, (
        f"Fixture: GitHub is paused, so the publication is still queued: {statuses(case)}")
    new = written_since(case.store, before)
    names = ids | {case.plan["id"]}

    def this_order(record):
        rest = {key: value for key, value in record.items() if key not in {"cause", "reason", "summary", "say"}}
        return any(text in names for text in strings(rest))

    named = cause_records(new, this_order)
    found = [record for record in named if "github" in cause_of(record).lower()]
    assert found, (
        "The owner's publication pass ended with the publication still queued because GitHub is paused, but no "
        f"record written in this pass names it with that cause. Records naming it with a cause or reason: "
        f"{named}. Records written in this pass: {new[:12]}")
    assert len(found) <= 2, (
        f"One owner pass (publication check and owner) records the cause at most once each: {found}")


# ---------------------------------------------------------------- record write failures

def visible_failures(store, word, output):
    texts = [_key(record) for record in kept_records(store)]
    texts += output
    return [text for text in texts
            if word in text.lower() and any(failure in text.lower() for failure in FAILURE_WORDS)]


def output_lines(capsys, caplog):
    captured = capsys.readouterr()
    return [captured.out, captured.err] + [record.getMessage() for record in caplog.records]


def test_a_failed_flow_record_write_leaves_a_visible_event_naming_the_failure(flow, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    store = flow.store
    flow.clock[0] = NOW
    store.record_flow("tick", status="ok", duration_seconds=1.0, steps=[])
    assert store.flow_records(), "Fixture: flow records are written normally"
    before = len(visible_failures(store, "flow", []))
    capsys.readouterr()
    path = store.root / "flow-records.jsonl"
    path.unlink()
    path.mkdir()  # The flow file can no longer be written.
    flow.clock[0] = NOW + timedelta(minutes=1)
    store.record_flow("ready", issue=7, run_started_at=None, reason="free-slot", outcome="waiting")
    path.rmdir()
    found = visible_failures(store, "flow", output_lines(capsys, caplog))
    assert len(found) > before, (
        "Writing #7's flow record failed, and nothing visible names the failure: the record was silently dropped")


def test_a_failed_phase_record_write_leaves_a_visible_event_naming_the_failure(flow, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    store = flow.store
    flow.clock[0] = NOW
    store.start_run(8, branch="foragent/issue-8", worktree=store.worktree_path(8))
    store.update_run(8, phase="implementing", phase_started_at=NOW.isoformat())
    journal = store.root / "orch-events.jsonl"
    journal.touch()
    before = len(visible_failures(store, "phase", []))
    capsys.readouterr()
    os.chmod(journal, 0o444)  # The phase journal can no longer be written.
    try:
        try:
            journal.open("a").close()
        except PermissionError:
            pass
        else:
            pytest.fail("Fixture: a read-only journal must refuse writes (do not run as root)")
        flow.clock[0] = NOW + timedelta(minutes=20)
        try:
            store.update_run(8, phase="reviewing", phase_started_at=flow.clock[0].isoformat())
        except Exception:  # noqa: BLE001 - a raised error is checked below, not accepted silently
            pass
    finally:
        os.chmod(journal, 0o644)
    found = visible_failures(store, "phase", output_lines(capsys, caplog))
    assert len(found) > before, (
        "Writing #8's 'implementing' phase record failed, and no event, flow record or log line names the failure")
