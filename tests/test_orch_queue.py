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
    for argv in (["--global", "orch"], ["--global", "4", "--clear"], []):
        with pytest.raises(SystemExit) as raised:
            cli.main(["orch", "queue", *argv, "--repo-root", str(tmp_path)])
        assert raised.value.code not in (0, None)
    assert orch_queue.get_global(StateStore(tmp_path)) == []


def test_global_priority_changes_are_recorded_once_and_a_corrupt_list_stays_replaceable(tmp_path):
    store = StateStore(tmp_path)
    orch_queue.set_global(store, [])  # Nothing changed: clearing an empty list is no fact.
    assert store.flow_records() == []
    orch_queue.set_global(store, [5, 4, 5])
    orch_queue.set_global(store, [5, 4])
    (tmp_path / "global-queue.json").write_text("not json", encoding="utf-8")
    assert orch_queue.set_global(store, [5, 4]) == [5, 4]
    orch_queue.set_global(store, [])
    assert [row["data"]["issues"] for row in store.flow_records()] == [[5, 4], [5, 4], []]
