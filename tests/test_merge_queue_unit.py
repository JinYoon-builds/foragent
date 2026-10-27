from datetime import UTC, datetime

import pytest

from foragent.config import ConfigError, load_config
from foragent.state import StateStore


def store_with_runs(tmp_path, *numbers):
    store = StateStore(tmp_path / "state", now=lambda: datetime(2026, 10, 26, tzinfo=UTC))
    for number in numbers:
        store.start_run(number, branch=f"foragent/issue-{number}", worktree=tmp_path / str(number))
    return store


def entry(number, base="main", head="a" * 40):
    return {"number": number, "head_sha": head, "base": base}


def test_enqueue_keeps_fifo_position_and_updates_head(tmp_path):
    store = store_with_runs(tmp_path, 1, 2)
    store.enqueue_merge(entry(1))
    store.enqueue_merge(entry(2))
    store.enqueue_merge(entry(1, head="b" * 40))
    assert [(row["number"], row["head_sha"][0]) for row in store.merge_queue()] == [(1, "b"), (2, "a")]


def test_leader_reserves_its_slot_and_claims_oldest_unclaimed_peers(tmp_path):
    store = store_with_runs(tmp_path, 1, 2, 3, 4, 5)
    for number in (1, 2, 3, 4):
        store.enqueue_merge(entry(number))
    store.enqueue_merge(entry(5, base="other"))
    assert [row["number"] for row in store.claim_merge_group(4, "main", 2)] == [1, 4]
    # A second leader groups only unclaimed entries.
    assert [row["number"] for row in store.claim_merge_group(3, "main", 4)] == [2, 3]
    assert store.claim_merge_group(5, "other", 4) == []
    store.release_merge_claims(4)
    assert store.has_unclaimed_merge_peers(3, "main")  # 1 is free again; 2 stays with 3.
    assert {row["number"]: row["claimed_by"] for row in store.merge_queue()}[1] is None


def test_readers_drop_entries_and_claims_of_stopped_runs(tmp_path):
    store = store_with_runs(tmp_path, 1, 2, 3)
    for number in (1, 2, 3):
        store.enqueue_merge(entry(number))
    store.claim_merge_group(3, "main", 2)
    store.finish_run(3, "retries-exceeded")
    store.finish_run(2, "merged")
    assert store.claim_merge_group(1, "main", 4) == []
    assert [row["number"] for row in store.merge_queue()] == [1]
    store.dequeue_merge(1)
    assert store.merge_queue() == []


@pytest.mark.parametrize("value", [0, -1, "2", 1.5, True])
def test_merge_group_max_must_be_a_positive_integer(tmp_path, value):
    (tmp_path / ".foragent.yml").write_text(f"repo: o/r\nmerge_group_max: {value!r}\n".replace("'", '"'))
    with pytest.raises(ConfigError, match="merge_group_max"):
        load_config(tmp_path, global_path=tmp_path / "none.yml")


def test_merge_group_max_defaults_to_four_and_has_no_upper_limit(tmp_path):
    (tmp_path / ".foragent.yml").write_text("repo: o/r\n")
    assert load_config(tmp_path, global_path=tmp_path / "none.yml").merge_group_max == 4
    (tmp_path / ".foragent.yml").write_text("repo: o/r\nmerge_group_max: 1000\n")
    assert load_config(tmp_path, global_path=tmp_path / "none.yml").merge_group_max == 1000


def test_a_claimed_member_cannot_lead_another_group_before_release(tmp_path):
    store = store_with_runs(tmp_path, 1, 2, 3)
    for number in (1, 2, 3):
        store.enqueue_merge(entry(number))
    assert [row["number"] for row in store.claim_merge_group(3, "main", 2)] == [1, 3]
    # Member 1 belongs to leader 3's group; re-enqueueing keeps that claim.
    store.enqueue_merge(entry(1, head="c" * 40))
    assert store.claim_merge_group(1, "main", 4) == []
    assert not store.has_unclaimed_merge_peers(1, "main")
    assert {row["number"]: row["claimed_by"] for row in store.merge_queue()} == {1: 3, 2: None, 3: 3}
    store.release_merge_claims(1)  # Releasing never touches another leader's claims.
    assert {row["number"]: row["claimed_by"] for row in store.merge_queue()} == {1: 3, 2: None, 3: 3}


@pytest.mark.parametrize("checks,expected", [("local", True), ("ci", False)])
def test_grouping_follows_the_configured_checks_mode(checks, expected):
    from types import SimpleNamespace

    from foragent.config import Config
    from foragent.run_merge_group import grouping_enabled

    # checks: ci excludes groups even when no CI comparison is possible (npm test).
    config = Config(checks=checks, test_command="npm test")
    attempt = SimpleNamespace(ctx=SimpleNamespace(config=config), target_branch="main", batch_branch=None)
    assert grouping_enabled(attempt, True) is expected
    assert grouping_enabled(attempt, False) is False


def test_a_restarted_execution_never_inherits_its_old_entry_claim_or_marker(tmp_path):
    clock = [datetime(2026, 10, 26, tzinfo=UTC)]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    for number in (1, 2):
        store.start_run(number, branch=f"foragent/issue-{number}", worktree=tmp_path / str(number))
        store.enqueue_merge(entry(number))
    store.claim_merge_group(2, "main", 2)
    store.record_group_merge(1, "a" * 40, store.get_run(1).started_at)
    assert store.group_merged_head(1) == "a" * 40
    # Both issues restart as new executions before either is ready again.
    from datetime import timedelta
    clock[0] += timedelta(minutes=5)
    store.start_run(1, branch="foragent/issue-1", worktree=tmp_path / "1")
    store.start_run(2, branch="foragent/issue-2", worktree=tmp_path / "2")
    assert store.group_merged_head(1) is None
    assert not store.has_unclaimed_merge_peers(2, "main")
    # The reader's cleanup is saved, not only computed.
    assert store.merge_queue() == []


def test_group_finish_keeps_local_facts_reinstall_and_journal_when_github_fails(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from foragent import runner
    from foragent.run_merge_group import finish_group
    from foragent.tracker import TrackerError

    store = store_with_runs(tmp_path, 1, 4, 5)
    reinstalls = []
    monkeypatch.setattr(runner, "after_merge", lambda ctx, pr, base: reinstalls.append(pr))

    class Tracker:
        def __init__(self):
            self.closed = []

        def current_issue(self, number):
            return SimpleNamespace(state="open")

        def close_issue(self, number, comment=None):
            if number == 1:
                raise TrackerError("HTTP 429: API rate limit exceeded")
            self.closed.append((number, comment))

        def remove_labels(self, number, labels):
            pass

    tracker = Tracker()
    monkeypatch.setattr("foragent.board.sync_status", lambda ctx, number, status: None)
    ctx = SimpleNamespace(store=store, tracker=tracker, now=store._now)
    attempt = SimpleNamespace(ctx=ctx, n=5, log=tmp_path / "5.log", target_branch="main")
    borrowed = [{"number": 1, "pr": 11, "head_sha": "1" * 40}, {"number": 4, "pr": 14, "head_sha": "4" * 40}]
    finish_group(attempt, borrowed, (15, "5" * 40), reinstall=True, queue=True)
    for number in (1, 4, 5):
        assert store.get_run(number).merge_group_size == 3
        assert store.get_run(number).merge_group_bisect_checks == 0
    assert reinstalls == [15]
    assert tracker.closed == [(4, "Merged in a merge group with #1, #5.")]
    events = [json.loads(line) for line in (store.root / "orch-events.jsonl").read_text().splitlines()]
    merges = [row for row in events if row["kind"] == "merge"]
    assert [row["issue"] for row in merges] == [1, 4, 5]
    assert "with #4, #5" in merges[0]["summary"] and "PR #11" in merges[0]["summary"]
    # The failed closure is queued for the outbox retry and reported, not swallowed.
    queued = [(row["issue"], row["action"]) for row in store.github_outbox()]
    assert queued == [(1, "close_issue"), (1, "remove_labels")]
    assert [(row["kind"], row["issue"]) for row in events if row["kind"] != "merge"] == [("close-pending", 1)]
    assert "queued for retry" in store.get_run(1).detail


def test_a_later_member_error_never_skips_finishing_an_earlier_leader_merge(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from foragent import run_merge_group
    from foragent.tracker import TrackerError

    merged = []

    def merge_member(attempt, member):
        if member.entry["number"] == 6:
            raise TrackerError("HTTP 429: API rate limit exceeded")
        merged.append(member.entry["number"])
        return True

    monkeypatch.setattr(run_merge_group, "merge_member", merge_member)
    attempt = SimpleNamespace(n=5)
    order = [{"number": 1}, {"number": 5}, {"number": 6}]
    members = {number: SimpleNamespace(entry={"number": number}) for number in (1, 6)}
    borrowed, leader_merged, error = run_merge_group.merge_in_order(attempt, order, members, lambda: True)
    assert [row["number"] for row in borrowed] == [1] and leader_merged is True
    assert isinstance(error, TrackerError) and error.is_rate_limit
    # A failing member before the leader stops the rest: they were checked only together.
    borrowed, leader_merged, error = run_merge_group.merge_in_order(
        attempt, [{"number": 6}, {"number": 5}], members, lambda: True)
    assert (borrowed, leader_merged) == ([], False) and error is not None


def test_a_stale_peer_is_dropped_before_its_branch_is_fetched(tmp_path):
    from types import SimpleNamespace

    from foragent import run_merge_group
    from foragent.git import GitError

    store = store_with_runs(tmp_path, 1, 5)
    (tmp_path / "1").mkdir()
    (tmp_path / "5").mkdir()
    for number in (1, 5):
        store.enqueue_merge({**entry(number), "branch": f"foragent/issue-{number}", "worktree": str(tmp_path / str(number))})
    fetched = []

    class Git:
        def fetch(self, remote, ref):
            if ref == "foragent/issue-1":
                raise GitError("couldn't find remote ref foragent/issue-1")  # merged alone, branch deleted
            fetched.append(ref)

        def ref_sha(self, ref):
            return "b" * 40

        def add_detached_worktree(self, path, ref):
            path.mkdir(parents=True)

        def at(self, root):
            return self

        def remove_worktree(self, path):
            pass

    config = SimpleNamespace(merge_group_max=4)
    ctx = SimpleNamespace(store=store, git=Git(), config=config)
    attempt = SimpleNamespace(n=5, ctx=ctx, target_branch="main", log=tmp_path / "5.log")
    assert run_merge_group.form(attempt) == (None, False)  # Proceeds alone; the leader is not failed.
    assert "#1 dropped as stale" in (tmp_path / "5.log").read_text()
    # The stale peer leaves the queue, so it is never claimed again.
    assert {row["number"]: row["claimed_by"] for row in store.merge_queue()} == {5: None}
    # A peer whose run restarted is dropped without touching its branch at all.
    store.start_run(1, branch="foragent/issue-1", worktree=tmp_path / "1")
    assert run_merge_group._stale(attempt, {**entry(1), "started_at": "old", "worktree": str(tmp_path / "1")})


def test_borrowed_lock_facts_are_written_while_merge_lock_is_held(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from foragent import run_publish

    store = store_with_runs(tmp_path, 1, 5)
    held = []
    lock = store.merge_lock

    @contextmanager
    def observed():
        with lock():
            held.append(True)
            yield
            held.append(False)

    monkeypatch.setattr(store, "merge_lock", observed)
    writes = []
    update = store.update_run

    def observe_update(number, **fields):
        if "merge_lock_held_seconds" in fields:
            writes.append((number, held[-1]))
        return update(number, **fields)

    monkeypatch.setattr(store, "update_run", observe_update)
    ctx = SimpleNamespace(store=store, now=store._now, monotonic=lambda: 0.0)
    hold = run_publish._HOLDS[5] = run_publish._Hold()
    with run_publish._merge_lock(5, ctx):
        hold.members.append(1)
    # The member waits on merge.lock, so a write made before release cannot be discarded.
    assert (1, True) in writes


def test_ci_mode_never_creates_or_reads_the_queue_file(tmp_path):
    from types import SimpleNamespace

    from foragent.config import Config
    from foragent.run_merge_group import queue_in_use

    store = store_with_runs(tmp_path, 1)
    assert queue_in_use(SimpleNamespace(config=Config(checks="ci"))) is False
    assert queue_in_use(SimpleNamespace(config=Config(merge_group_max=1))) is False
    assert queue_in_use(SimpleNamespace(config=Config())) is True
    # Even an unguarded removal never creates the file just to say nothing is queued.
    store.dequeue_merge(1)
    store.release_merge_claims(1)
    assert not (store.root / "merge-queue.json").exists()


def test_a_peer_whose_worktree_is_gone_is_never_selected_again(tmp_path):
    store = store_with_runs(tmp_path, 1, 5)
    (tmp_path / "5").mkdir()
    for number in (1, 5):
        store.enqueue_merge({**entry(number), "worktree": str(tmp_path / str(number))})
    # 1 still has a running record, but its worktree no longer exists.
    assert not store.has_unclaimed_merge_peers(5, "main")
    assert store.claim_merge_group(5, "main", 4) == []
    assert [row["number"] for row in store.merge_queue()] == [5]


def test_forming_a_group_after_another_group_merged_this_run_finishes_instead_of_failing(tmp_path):
    from types import SimpleNamespace

    from foragent import run_merge_group
    from foragent.git import GitError

    store = store_with_runs(tmp_path, 1, 5)
    for number in (1, 5):
        (tmp_path / str(number)).mkdir()
        store.enqueue_merge({**entry(number), "branch": f"foragent/issue-{number}",
                             "worktree": str(tmp_path / str(number))})
    store.record_group_merge(5, "a" * 40, store.get_run(5).started_at)  # An earlier group merged 5.

    class Git:
        def fetch(self, remote, ref):
            if ref == "foragent/issue-5":
                raise GitError("couldn't find remote ref foragent/issue-5")  # GitHub deleted it.

        def ref_sha(self, ref):
            return "b" * 40

        def add_detached_worktree(self, path, ref):
            path.mkdir(parents=True)

        def at(self, root):
            return self

        def remove_worktree(self, path):
            pass

    ctx = SimpleNamespace(store=store, git=Git(), config=SimpleNamespace(merge_group_max=4))
    attempt = SimpleNamespace(n=5, ctx=ctx, target_branch="main", log=tmp_path / "5.log")
    assert run_merge_group.form(attempt) == (None, False)
    assert all(row["claimed_by"] is None for row in store.merge_queue())


def test_a_group_on_a_moved_main_also_protects_tests_that_pass_there(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from foragent import run_baseline, run_merge_group

    captured = []

    def capture(n, ctx, test_command, target_ref):
        captured.append((n, test_command, target_ref))
        return SimpleNamespace(node_ids=("tests/acceptance/test_new.py::test_new",))

    monkeypatch.setattr(run_baseline, "_capture_acceptance_baseline", capture)
    attempt = SimpleNamespace(n=5, ctx=SimpleNamespace(), log=tmp_path / "5.log")
    same = [{"baseline_base_sha": "b" * 40, "baseline_test_command": "pytest"}] * 2
    assert run_merge_group._baseline_on(attempt, same, "b" * 40) == set() and captured == []
    # Another group merged the owner of a pending acceptance test: main moved.
    moved = [{"baseline_base_sha": "a" * 40, "baseline_test_command": "pytest"}, same[0]]
    assert run_merge_group._baseline_on(attempt, moved, "b" * 40) == {"tests/acceptance/test_new.py::test_new"}
    assert captured == [(5, "pytest", "b" * 40)]

    def unavailable(*args):
        raise RuntimeError("acceptance baseline unavailable")

    monkeypatch.setattr(run_baseline, "_capture_acceptance_baseline", unavailable)
    assert run_merge_group._baseline_on(attempt, moved, "b" * 40) is None  # The group then fails over.


def test_the_joint_check_keeps_every_members_own_unit_selection(tmp_path):
    from types import SimpleNamespace

    from foragent.config import Config
    from foragent import run_merge_group

    (tmp_path / "tests").mkdir()
    for name in ("test_importer.py", "test_member_4.py"):
        (tmp_path / "tests" / name).write_text("def test_ok():\n    pass\n")

    class Git:
        def changed_paths(self, base):
            return ["tests/test_member_4.py"]  # The combined view no longer selects the importer.

    from foragent.run_checks import _pytest_command

    config = Config(test_command="pytest -q")
    attempt = SimpleNamespace(ctx=SimpleNamespace(config=config))
    command = _pytest_command("pytest -q")  # What each member stored from its own verification.
    entries = [{"unit_command": command, "unit_tests": ["tests/test_importer.py"]},
               {"unit_command": command, "unit_tests": ["tests/test_member_4.py", "tests/test_gone.py"]}]
    runs = run_merge_group._unit_runs(attempt, entries, tmp_path, Git(), "b" * 40)
    assert runs == {command: ["tests/test_importer.py", "tests/test_member_4.py"]}


def test_a_group_merged_run_finishes_only_after_acquiring_merge_lock(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from foragent import run_merge_group, run_publish

    store = store_with_runs(tmp_path, 1)
    order = []
    lock = store.merge_lock

    @contextmanager
    def observed():
        order.append("lock")  # The leader held it until it saved this run's facts.
        with lock():
            yield

    monkeypatch.setattr(store, "merge_lock", observed)
    monkeypatch.setattr(run_merge_group, "merged_by_group", lambda attempt, pr, head: True)
    monkeypatch.setattr(run_merge_group, "finish_merged_self", lambda attempt: order.append("finish"))
    ctx = SimpleNamespace(store=store, now=store._now, monotonic=lambda: 0.0)
    attempt = SimpleNamespace(n=1, ctx=ctx)
    assert run_publish._finish_if_group_merged(attempt, 11, "a" * 40, locked=False) is True
    assert order == ["lock", "finish"]
    assert store.get_run(1).merge_lock_held_seconds is None  # This run's own empty hold is not recorded.
