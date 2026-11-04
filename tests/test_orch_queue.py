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


def test_global_list_is_separate_from_lane_queues_and_clears(tmp_path):
    store = StateStore(tmp_path)
    orch_queue.set_order(store, "orch", [2])
    assert orch_queue.get_global(store) == []
    assert orch_queue.set_global(store, [85, 45, 85]) == [85, 45]
    assert orch_queue.get_global(StateStore(tmp_path)) == [85, 45]
    assert orch_queue.set_global(store, []) == []
    assert not (tmp_path / "global-queue.json").exists()
    assert orch_queue.get(store, "orch") == [2]


def test_global_queue_cli_rejects_non_numbers_and_mixed_clear(tmp_path, monkeypatch):
    import pytest

    from foragent import cli
    from foragent.config import Config

    monkeypatch.setattr(cli, "load_config", lambda root: Config(repo="o/r"))
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo, **kwargs: cls(tmp_path)))
    for argv in (["--global", "orch"], ["--global", "4", "--clear"], [],
                 ["--global", "4", "--add", "5"], ["--global", "4", "--remove", "5"],
                 ["--global", "--add", "4", "--remove", "5"],
                 ["--global", "--add", "4", "--clear"],
                 ["--global", "--remove", "4", "--clear"],
                 ["orch", "--add", "4"], ["orch", "--remove", "4"]):
        with pytest.raises(SystemExit) as raised:
            cli.main(["orch", "queue", *argv, "--repo-root", str(tmp_path)])
        assert raised.value.code not in (0, None)
    assert orch_queue.get_global(StateStore(tmp_path)) == []


def test_global_changes_record_only_real_changes_and_keep_lane_queue(tmp_path):
    store = StateStore(tmp_path)
    orch_queue.set_order(store, "orch", [2])
    assert orch_queue.change_global(store, 7) == [7]
    assert orch_queue.change_global(store, 3) == [7, 3]
    assert orch_queue.change_global(store, 7) == [7, 3]
    assert orch_queue.change_global(store, 999, remove=True) == [7, 3]
    assert orch_queue.change_global(store, 7, remove=True) == [3]
    assert orch_queue.change_global(store, 3, remove=True) == []
    assert not (tmp_path / "global-queue.json").exists()
    assert orch_queue.get(store, "orch") == [2]
    assert [row["data"]["issues"] for row in store.flow_records() if row["kind"] == "priority"] == [
        [7], [7, 3], [3], []]


def test_global_changes_hold_state_lock_across_read_and_write(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, current_thread

    store = StateStore(tmp_path)
    orch_queue.set_global(store, [7])
    read, release, contender_started, contender_read = Event(), Event(), Event(), Event()
    original = orch_queue.get_global

    def paused_read(store):
        order = original(store)
        if current_thread().name.startswith("first"):
            read.set()
            assert release.wait(5)
        else:
            contender_read.set()
        return order

    def second_change():
        contender_started.set()
        return orch_queue.change_global(StateStore(tmp_path), 12)

    monkeypatch.setattr(orch_queue, "get_global", paused_read)
    with ThreadPoolExecutor(1, thread_name_prefix="first") as first, ThreadPoolExecutor(1) as second:
        one = first.submit(orch_queue.change_global, store, 3)
        try:
            assert read.wait(5)
            two = second.submit(second_change)
            assert contender_started.wait(5)
            assert not contender_read.wait(0.2)
        finally:
            release.set()
        assert one.result() == [7, 3]
        assert two.result() == [7, 3, 12]
    assert original(store) == [7, 3, 12]
