"""Unknown fields written by a newer version survive every write-back."""

import json

from foragent.inspect_data import Prepared, PreparedCommand, load_prepared
from foragent.inspect_runtime import _prepared_write_back
from foragent.records import write_back
from foragent.state import Approval, StateStore

EXTRA = {"future_record_metadata": {"schema": 99}}


def seed(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_write_back_keeps_unknown_fields_and_replaces_known_ones():
    saved = {"number": 7, "body": "old", **EXTRA}
    assert write_back(Approval, saved, {"number": 7, "body": "new"}) == {"number": 7, "body": "new", **EXTRA}
    assert write_back(Approval, None, {"number": 7}) == {"number": 7}


def test_save_approval_round_trip_keeps_unknown_fields(tmp_path):
    store = StateStore(tmp_path)
    path = tmp_path / "approvals/7.json"
    seed(path, {"number": 7, "body": "b", "body_sha": "s", "acceptance_blobs": {},
                "approved_by": "human", "approved_at": "t", **EXTRA})
    loaded = store.load_approval(7)
    store.save_approval(7, body=loaded.body, body_sha="s2", acceptance_blobs={},
                        approved_by=loaded.approved_by, approved_at=loaded.approved_at)
    assert read(path)["future_record_metadata"] == EXTRA["future_record_metadata"]
    assert store.load_approval(7).body_sha == "s2"


def test_save_flag_round_trip_keeps_unknown_fields(tmp_path):
    store = StateStore(tmp_path)
    path = tmp_path / "flags/7.json"
    seed(path, {"number": 7, "reason": "stalled", "flagged_at": "t", **EXTRA})
    flag = store.load_flag(7)
    store.save_flag(7, "spec-question", flagged_at=flag.flagged_at)
    assert read(path) == {"number": 7, "reason": "spec-question", "flagged_at": "t", **EXTRA}


def test_update_inspect_keeps_unknown_fields(tmp_path):
    store = StateStore(tmp_path)
    seed(tmp_path / "inspect.json", {"since": "a", **EXTRA})
    store.update_inspect(since="b")
    assert read(tmp_path / "inspect.json")["future_record_metadata"] == EXTRA["future_record_metadata"]


def test_prepared_load_reads_outer_record_tolerantly(tmp_path):
    store = StateStore(tmp_path)
    seed(tmp_path / "inspect/prepared/7.json", {
        "base_sha": "base", "results": [{"command": "true", "output": "", "exit_code": 0, **EXTRA}], **EXTRA,
    })
    assert load_prepared(store, 7) == Prepared("base", [PreparedCommand("true", "", 0)])


def test_reprepared_inspection_keeps_outer_and_matching_command_unknown_fields(tmp_path):
    path = tmp_path / "prepared.json"
    seed(path, {"base_sha": "old", **EXTRA, "results": [
        {"command": "true", "output": "x", "exit_code": 0, "nested": 1},
        {"command": "gone", "output": "", "exit_code": 0, "nested": 2},
    ]})
    record = _prepared_write_back(path, Prepared("new", [PreparedCommand("true", "y", 0), PreparedCommand("new", "", 1)]))
    assert record["base_sha"] == "new" and record["future_record_metadata"] == EXTRA["future_record_metadata"]
    assert record["results"][0]["nested"] == 1 and record["results"][0]["output"] == "y"
    assert "nested" not in record["results"][1]
    assert _prepared_write_back(tmp_path / "missing.json", Prepared("b", []))["results"] == []


def test_reprepared_repeated_commands_keep_their_own_unknown_fields_in_order(tmp_path):
    path = tmp_path / "prepared.json"
    seed(path, {"base_sha": "old", "results": [
        {"command": "true", "output": "", "exit_code": 0, "nested": "first"},
        {"command": "true", "output": "", "exit_code": 0, "nested": "second"},
    ]})
    commands = [PreparedCommand("true", "", 0)] * 3
    record = _prepared_write_back(path, Prepared("new", commands))
    assert [item.get("nested") for item in record["results"]] == ["first", "second", None]


def test_run_resumed_by_start_run_after_limit_wait_keeps_unknown_fields(tmp_path):
    store = StateStore(tmp_path)
    store.start_run(7, branch="foragent/issue-7", worktree=tmp_path / "wt")
    path = tmp_path / "runs/7.json"
    seed(path, {**read(path), **EXTRA})
    store.update_run(7, attempts=2)
    store.finish_run(7, "limit-wait", "waiting for the executor limit")
    history = [json.loads(line) for line in (tmp_path / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert history[-1]["future_record_metadata"] == EXTRA["future_record_metadata"]

    resumed = store.start_run(7, branch="foragent/issue-7", worktree=tmp_path / "wt")
    assert resumed.attempts == 2 and resumed.status == "running"
    assert read(path)["future_record_metadata"] == EXTRA["future_record_metadata"]
    assert read(path)["status"] == "running"
