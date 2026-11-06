"""A step that makes work available wakes the gate once, and only when it added work."""

from foragent import main_write_requests
from foragent.state import StateStore


def test_a_new_main_write_request_wakes_the_gate_and_a_repeat_does_not(tmp_path, kicks):
    store = StateStore(tmp_path / "state")
    payload = {"kind": "notes", "notes": "n", "batch": "b1"}
    first = main_write_requests.submit(store, "main", "Notes", "orchestrator", payload,
                                       identity="notes:b1", wake=tmp_path)
    assert kicks == [tmp_path], "a queued request must start a pass for the merge owner"
    again = main_write_requests.submit(store, "main", "Notes", "orchestrator", payload,
                                       identity="notes:b1", wake=tmp_path)
    assert again["id"] == first["id"]
    assert kicks == [tmp_path], "an already queued request asks for no further pass"


def test_a_request_without_a_wake_root_starts_nothing(tmp_path, kicks):
    store = StateStore(tmp_path / "state")
    main_write_requests.submit(store, "main", "Notes", "orchestrator", {"kind": "notes"}, identity="x")
    assert kicks == []
