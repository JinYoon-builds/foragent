"""Legacy order journal entries are migrated in a reversible way (follow-up of #2483).

A batch queued before the upgrade sits in orders.jsonl in the old format: each row
holds the complete plan and its retained files. The batch is then resumed through
the public `fa order flush` control. After it is published, orders.jsonl must keep
no complete plan for those orders, and running the migration's restore step,
`fa order restore-journal`, must bring back every original legacy journal entry
byte for byte.

How the migration keeps its copy (file, archive or record) is not a contract: the
tests observe the kept copy only through the restore step.
"""

from datetime import UTC, datetime
import json

import pytest

from test_d85_batched_publication import BOUNDARY, DECISION, IDEA, call, prepare
from test_d85_flush_recovery import (  # noqa: F401
    Interrupted, append, finish, offline_publication, plan, publishing, recovery, stop,
)
from foragent.state import StateStore
from test_order_preread import preread_case  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


def legacy_row(value, *, approve):
    """The exact row the pre-#2483 writer appended to orders.jsonl for `publish --defer`."""
    files = {name: content for item in value["issues"] for name, content in item["acceptance_files"].items()}
    return {"id": value["id"], "plan": value, "approve": approve, "approve_keys": [],
            "approve_numbers": [], "boundary": BOUNDARY,
            "recorded_at": datetime.now(UTC).isoformat(), "status": "waiting", "files": files}


def queue_legacy_batch(case, capsys):
    """Queue two orders and three notes, the orders in the old journal format."""
    store = StateStore.for_repo("owner/repo")
    values, lines = [], []
    for identity, approve in (("first", True), ("second", False)):
        value, _path = prepare(case, capsys, identity)
        payload = "\n" + json.dumps(legacy_row(value, approve=approve), ensure_ascii=False) + "\n"
        store.root.mkdir(parents=True, exist_ok=True)
        with (store.root / "orders.jsonl").open("ab") as stream:
            stream.write(payload.encode("utf-8"))
        values.append(value)
        lines.append(payload.strip("\n").encode("utf-8"))
    append(case, capsys, "decision", DECISION)
    append(case, capsys, "idea", IDEA)
    call(case, capsys, "note", "--defer", BOUNDARY, "--kind", "rule",
         "--text", "- Retain waiting work until publication. (D85)",
         "--replace", "- Publish each order immediately. (D85)")
    shown = json.loads(call(case, capsys, "pending", "--json"))
    waiting = {row["id"] for row in shown["orders"] if row["status"] == "waiting"}
    assert waiting == {value["id"] for value in values}, "The legacy rows must read as a queued batch"
    return tuple(values), tuple(lines)


def json_objects(value):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from json_objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from json_objects(item)


def documents(path):
    """Every JSON line of a journal file."""
    found = []
    for line in path.read_bytes().splitlines():
        try:
            found.append(json.loads(line))
        except ValueError:
            continue
    return found


def journal_path():
    return StateStore.for_repo("owner/repo").root / "orders.jsonl"


def copies_in_journal(value):
    """How many times the complete plan appears anywhere in orders.jsonl."""
    path = journal_path()
    if not path.exists():
        return 0
    return sum(1 for document in documents(path) for item in json_objects(document) if item == value)


def restore(case, capsys):
    """Run the migration's public restore step."""
    return call(case, capsys, "restore-journal")


def assert_restored(lines):
    present = journal_path().read_bytes().splitlines()
    for line in lines:
        assert present.count(line) == 1, (
            "The restore step must bring back each original legacy journal entry byte for byte, "
            f"exactly once; found it {present.count(line)} time(s)")


def journal_plans(value):
    rows = documents(journal_path()) if journal_path().exists() else []
    return [row for row in rows if isinstance(row, dict) and row.get("id") == value["id"]
            and isinstance(row.get("plan"), dict) and "issues" in row["plan"]]


def interrupt(case, monkeypatch, edge):
    with monkeypatch.context() as patch:
        if edge == "push":
            original = case.git_adapter.push_base

            def crash(branch):
                original(branch)
                raise Interrupted()

            patch.setattr(case.git_adapter, "push_base", crash)
        else:
            original = case.tracker.create_issue

            def crash(*args, **kwargs):
                number = original(*args, **kwargs)
                if len(case.tracker.list_issues()) == 2:
                    raise Interrupted()
                return number

            patch.setattr(case.tracker, "create_issue", crash)
        stop(case, case.capsys)


@pytest.mark.parametrize("edge", ["not-started", "push", "issue"])
def test_resumed_legacy_batch_leaves_no_plan_in_the_order_journal_and_restores_byte_for_byte(
    recovery, capsys, monkeypatch, edge,
):
    case = recovery
    case.capsys = capsys
    values, lines = queue_legacy_batch(case, capsys)
    for value in values:
        assert copies_in_journal(value) == 1, "The legacy row holds the complete plan"
    if edge != "not-started":
        interrupt(case, monkeypatch, edge)
    finish(case, capsys, values)
    for value in values:
        assert journal_plans(value) == [], (
            f"{value['id']}: a published legacy order must not keep its complete plan in orders.jsonl")
        assert copies_in_journal(value) == 0, (
            f"{value['id']}: no copy of the published plan may remain anywhere in orders.jsonl")
    present = journal_path().read_bytes().splitlines()
    assert not set(lines) & set(present), "The migration must change the legacy entries it slims"
    restore(case, capsys)
    assert_restored(lines)


def test_restore_still_works_after_later_publication_controls(recovery, capsys):
    case = recovery
    values, lines = queue_legacy_batch(case, capsys)
    finish(case, capsys, values)
    call(case, capsys, "pending", "--json")
    call(case, capsys, "flush", "--boundary", BOUNDARY)
    for value in values:
        assert journal_plans(value) == [], (
            f"{value['id']}: reading or flushing again must not bring the plan back by itself")
    restore(case, capsys)
    assert_restored(lines)
