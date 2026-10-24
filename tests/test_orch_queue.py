from foragent import orch_queue
from foragent.state import StateStore


def test_ordered_puts_queue_first_then_number_order(tmp_path):
    store = StateStore(tmp_path)
    orch_queue.set_order(store, "orch", [7, 3, 7, 999])
    assert orch_queue.get(store, "orch") == [7, 3, 999]
    assert orch_queue.ordered(store, "orch", [5, 3, 7, 1]) == [7, 3, 1, 5]
    assert orch_queue.ordered(store, "main", [5, 3]) == [3, 5]


def test_clear_removes_only_that_lane(tmp_path):
    store = StateStore(tmp_path)
    orch_queue.set_order(store, "orch", [2])
    orch_queue.set_order(store, "view", [4])
    assert orch_queue.set_order(store, "orch", []) == []
    assert orch_queue.get(store, "orch") == []
    assert StateStore(tmp_path) and orch_queue.get(StateStore(tmp_path), "view") == [4]
