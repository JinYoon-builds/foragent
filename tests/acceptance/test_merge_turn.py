"""Acceptance test for merge-turn. Locked by Foragent; do not edit."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
import json
from threading import Condition, Event, RLock, local

import pytest

from conftest import git_command
from foragent import runner
from foragent.issue import body_hash
from test_base_move_reuse import (  # noqa: F401
    REUSED, REVERIFY, move_during_publish, publish_case,
)
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401


JOIN_ORDER = (5, 1, 4)  # Neither issue-number order nor run-start order.


def queue_bytes(case):
    queue = case.store.root / "merge-queue.jsonl"
    return queue.read_bytes() if queue.exists() else b""


def entries(raw):
    return [json.loads(line) for line in raw.splitlines()]


@pytest.fixture
def turn_case(publish_case, monkeypatch):
    """Reuse make_case's runner, offline Git transport, checks and fake tracker.

    Each PR adds independent source code, so every fake merge really advances
    main with a result-affecting change. Only the Git transport is serialized:
    the real publication code and StateStore.merge_lock decide who can merge.
    """
    case = publish_case
    # #459 (2026-10-30): merge groups default on; this file checks the one-PR FIFO turn.
    object.__setattr__(case.config, "merge_group_max", 1)
    case.worker = local()
    case.merges = []
    case.wake = lambda: None
    transport_lock = RLock()
    transport = case.git._run

    def serialized_transport(args):
        with transport_lock:
            return transport(args)

    monkeypatch.setattr(case.git, "_run", serialized_transport)
    approval = case.store.load_approval(1)
    for number in JOIN_ORDER:
        body = approval.body.replace("feature.txt", f"feature-{number}.txt")
        title = f"Independent source change {number}"
        if number == 1:
            case.tracker.update_issue(number, title=title, body=body)
        else:
            case.tracker.add_issue(number, title, body, labels=["approved", "running"])
        case.store.save_approval(
            number, body=body, body_sha=body_hash(body),
            acceptance_blobs=approval.acceptance_blobs,
            approved_by="human", approved_at=case.clock[0].isoformat(),
        )
        if number != 1:
            case.store.start_run(
                number, branch=f"foragent/issue-{number}",
                worktree=case.store.worktree_path(number),
            )

    def implement(kwargs):
        number = case.worker.number
        (kwargs["worktree"] / f"feature-{number}.txt").write_text("done\n")
        (kwargs["worktree"] / f"src/turn_{number}.py").write_text(f"VALUE = {number}\n")
        return "Implemented independent source change"

    case.implementer.steps = [implement] * len(JOIN_ORDER)
    case.reviewer.steps = ["VERDICT: PASS"] * len(JOIN_ORDER)
    create_pr, merge_pr = case.tracker.create_pr, case.tracker.merge_pr

    def create(**kwargs):
        pr = create_pr(**kwargs)
        number = case.worker.number
        case.tracker.set_pr_files(pr, [f"feature-{number}.txt", f"src/turn_{number}.py"])
        case.tracker.set_pr_checks(pr, "pass")
        return pr

    def merge(pr, *, expected_head_sha, **kwargs):
        before = case.history.tip
        raw = queue_bytes(case)
        # Never let the fake tracker install an old tree over a previous merge.
        git_command(case.history.origin, "merge-base", "--is-ancestor", before, expected_head_sha)
        merge_pr(pr, expected_head_sha=expected_head_sha, **kwargs)
        git_command(case.history.origin, "update-ref", "refs/heads/main", expected_head_sha)
        case.history.tip = expected_head_sha
        case.merges.append({
            "n": case.worker.number, "base": before, "head": expected_head_sha, "queue": raw,
        })
        case.wake()

    def run(number):
        case.worker.number = number
        try:
            return runner.run_issue(number, case.ctx)
        finally:
            # Wake readers after run_issue persists the finished run as well
            # as after the tracker merge, without mocking queue membership.
            case.wake()

    case.run = run
    monkeypatch.setattr(case.tracker, "create_pr", create)
    monkeypatch.setattr(case.tracker, "merge_pr", merge)
    return case


def assert_clean_merge(case, number, outcome):
    log = case.store.log_path(number).read_text()
    assert outcome == "merged", f"Run {number} must merge, got {outcome}\n{log}"
    assert case.store.get_run(number).outcome == "merged"
    assert case.tracker.get_issue(number).state == "closed"
    assert not case.tracker.get_issue(number).labels & {"blocked", "needs-human"}
    assert case.store.load_flag(number) is None, f"Run {number} must not be flagged"
    assert "base kept moving" not in log, f"Run {number} must stop only at its time budget"
    assert log.count("re-verifying") <= 2, f"Run {number} re-verified more than twice\n{log}"
    return log


def test_three_ready_runs_merge_in_fifo_order_without_reverification_churn(turn_case, monkeypatch):
    case = turn_case
    in_ci = {n: Event() for n in JOIN_ORDER}
    green = {n: Event() for n in JOIN_ORDER}
    settled = {n: Event() for n in JOIN_ORDER}
    release_first = Event()
    changed = Condition()
    generation = [0]
    checks, merge_lock = case.tracker.pr_checks, case.store.merge_lock

    def wake():
        with changed:
            generation[0] += 1
            changed.notify_all()

    def poll(pr):
        number = case.worker.number
        if not in_ci[number].is_set():
            in_ci[number].set()
            assert green[number].wait(20), f"Run {number} never received its fake green CI"
        return checks(pr)

    @contextmanager
    def delayed_first_lock():
        if case.worker.number == JOIN_ORDER[0]:
            settled[case.worker.number].set()
            assert release_first.wait(20), "The first ready run's lock wait was not released"
        with merge_lock():
            yield

    def sleep(seconds):
        # A FIFO follower waits here; the old lock race instead merges it.
        # Notifications report external progress and impose no merge order.
        settled[case.worker.number].set()
        with changed:
            seen = getattr(case.worker, "generation", 0)
            assert changed.wait_for(lambda: generation[0] > seen, timeout=20), (
                f"Run {case.worker.number} waited without merge progress"
            )
            case.worker.generation = generation[0]
            case.clock[0] += timedelta(seconds=seconds)

    def run(number):
        try:
            return case.run(number)
        finally:
            settled[number].set()

    case.wake = wake
    case.ctx.sleep = sleep
    monkeypatch.setattr(case.tracker, "pr_checks", poll)
    monkeypatch.setattr(case.store, "merge_lock", delayed_first_lock)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {}
        try:
            # All three verify on the same base before any CI response is
            # released. Then admit ready runs in a deliberate FIFO order.
            for number in JOIN_ORDER:
                futures[number] = pool.submit(run, number)
                assert in_ci[number].wait(20), f"Run {number} did not reach fake CI"
            assert case.merges == [], "All three runs must be verified before the first merge"
            for number in JOIN_ORDER:
                green[number].set()
                assert settled[number].wait(20), f"Ready run {number} neither queued nor finished"
        finally:
            for gate in green.values():
                gate.set()
            release_first.set()
            wake()
        outcomes = {n: future.result(timeout=40) for n, future in futures.items()}

    logs = {n: assert_clean_merge(case, n, outcomes[n]) for n in JOIN_ORDER}
    turns = [row for row in entries(queue_bytes(case)) if row["event"] == "turn"]
    assert {turn["n"] for turn in turns} == set(JOIN_ORDER), (
        "Each merged run must append a turn record to merge-queue.jsonl"
    )
    bases = {row["n"]: row["base"] for row in case.merges}
    for turn in turns:
        assert turn.get("members") == [turn["n"]], (
            "Default merge_turn_size=1 requires a members list naming only the turn holder"
        )
        assert turn.get("base_sha") == bases[turn["n"]], (
            f"Run {turn['n']}'s turn must record the base SHA it verified against"
        )
    for merge in case.merges:
        assert any(turn in entries(merge["queue"]) for turn in turns if turn["n"] == merge["n"]), (
            f"Run {merge['n']} must append its turn record before merging"
        )
    actual = [row["n"] for row in case.merges]
    assert actual == list(JOIN_ORDER), (
        f"Ready runs must merge in FIFO join order {list(JOIN_ORDER)}; got {actual}"
    )
    ready = [row["n"] for row in entries(queue_bytes(case)) if row["event"] == "ready"]
    assert list(dict.fromkeys(ready)) == list(JOIN_ORDER), "Each run must append its own ready entry"
    for row in case.merges:
        source = f"src/turn_{row['n']}.py"
        paths = git_command(case.history.origin, "diff", "--name-only", row["base"], row["head"])
        assert source in paths.splitlines(), "Every merge must advance main with source code"
    assert all(REVERIFY in logs[n] for n in JOIN_ORDER[1:]), "Successors must verify the changed source base"


def test_merge_queue_only_appends_and_each_run_writes_its_own_entries(turn_case):
    case = turn_case
    # Deliberate whitespace detects a reader/writer that normalizes old lines.
    original = (
        f'{{ "n": 90, "head_sha": "{case.base_sha}", "event": "ready" }}\n'
        f'{{ "n": 90, "head_sha": "{case.base_sha}", "event": "withdrawn" }}\n'
    ).encode()
    (case.store.root / "merge-queue.jsonl").write_bytes(original)
    previous = original
    for number in JOIN_ORDER:
        assert_clean_merge(case, number, case.run(number))
        before_merge = case.merges[-1]["queue"]
        after = queue_bytes(case)
        assert before_merge.startswith(previous), "Joining must preserve every prior byte of merge-queue.jsonl"
        own = entries(before_merge[len(previous):])
        assert any(row["n"] == number and row["event"] == "ready" for row in own), (
            f"Run {number} must append its own ready entry before merging"
        )
        assert after.startswith(before_merge), "A merge must retain all pre-merge queue lines byte for byte"
        appended = entries(after[len(previous):])
        assert all(row["n"] == number for row in appended), "A run may append only its own queue events"
        ready = next(row for row in own if row["event"] == "ready")
        assert ready["head_sha"] == case.merges[-1]["head"], "Readiness must name the published head"
        assert len(after) > len(previous), "merge-queue.jsonl must grow for every ready run"
        previous = after


def test_finished_and_crashed_queue_owners_are_skipped_without_deletion(turn_case):
    case = turn_case
    dead = []
    for number, outcome in ((90, "merged"), (91, "crashed")):
        case.store.start_run(
            number, branch=f"foragent/issue-{number}", worktree=case.store.worktree_path(number),
        )
        case.store.finish_run(number, outcome=outcome)
        dead.append({"n": number, "head_sha": case.base_sha, "event": "ready"})
    original = b"".join((json.dumps(row) + "\n").encode() for row in dead)
    (case.store.root / "merge-queue.jsonl").write_bytes(original)

    assert_clean_merge(case, 1, case.run(1))
    before_merge = case.merges[0]["queue"]
    assert any(row["n"] == 1 and row["event"] == "ready" for row in entries(before_merge)), (
        "The next live run must join the persisted queue before taking the merge turn"
    )
    assert before_merge.startswith(original), "A reader must skip dead ready entries without deleting them"
    assert queue_bytes(case).startswith(before_merge), "Skipping dead owners and merging must retain all old bytes"
    assert [row["n"] for row in case.merges] == [1], "The first live owner must get the turn"
    assert [(case.store.get_run(n).status, case.store.get_run(n).outcome) for n in (90, 91)] == [
        ("finished", "merged"), ("finished", "crashed"),
    ], "Queue readers must not rewrite dead owners' run records"


def test_regression_single_run_reuses_verification_for_a_neutral_base_move(publish_case, monkeypatch):
    case = publish_case
    move_during_publish(case, monkeypatch, [[{"docs/merge-turn.md": "Unrelated documentation\n"}]])
    assert runner.run_issue(1, case.ctx) == "merged"
    log = case.store.log_path(1).read_text()
    assert len(case.moves) == 1
    assert case.verifications == [case.base_sha], "A neutral base move must reuse the existing verification"
    assert REUSED in log and REVERIFY not in log
    assert len(case.tracker.merged_prs) == 1
