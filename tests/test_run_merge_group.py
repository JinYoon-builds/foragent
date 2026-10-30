"""Single-writer merge groups (#459): who may join, reuse a verdict, and reinstall."""

from datetime import UTC, datetime
import json
from types import SimpleNamespace

from foragent.config import Config
from foragent.run_merge_group import Membership
from foragent.run_merge_turn import QUEUE, Seat
from foragent.state import StateStore


def setup(tmp_path, *numbers, **config):
    store = StateStore(tmp_path / "state", now=lambda: datetime(2026, 10, 30, tzinfo=UTC))
    for n in numbers:
        store.start_run(n, branch=f"foragent/issue-{n}", worktree=tmp_path / str(n))
    ctx = SimpleNamespace(store=store, config=Config(**config), now=store._now)
    return store, ctx


def seat(store, n):
    return Seat(n, store.get_run(n).started_at, "main")


def write(store, *rows):
    with (store.root / QUEUE).open("a", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")


def ready(store, n, head, **fields):
    return {"n": n, "event": "ready", "started_at": store.get_run(n).started_at, "target": "main",
            "head_sha": head, "group": True, "checks": "local", "auto": True, **fields}


def claim(store, claim_id, leader, members):
    return {"event": "group", "id": claim_id, "leader": leader, "target": "main", "base_sha": "b" * 40,
            "members": [{"n": n, "head_sha": head} for n, head in members],
            "started_at": [store.get_run(n).started_at for n, _ in members]}


def membership(ctx, n, store):
    attempt = SimpleNamespace(ctx=ctx, n=n, target_branch="main", batch_branch=None)
    group = Membership(attempt, seat(store, n))
    group.automatic = True
    return group


def test_a_live_open_claim_reserves_members_and_a_dead_leaders_claim_does_not(tmp_path):
    store, ctx = setup(tmp_path, 1, 4, 5, 6, 7)
    write(store, *(ready(store, n, f"{n}" * 40) for n in (1, 4, 5)),
          ready(store, 6, "6" * 40, checks="ci"), ready(store, 7, "7" * 40, group=False),
          claim(store, "open", 1, [(1, "1" * 40), (4, "4" * 40)]))
    from foragent.run_merge_group import rows

    picked = membership(ctx, 5, store)._candidates(rows(store), 3)
    # Ineligible owners (ci mode, no automatic merge) never join; 1 and 4 belong to 1's open claim.
    assert picked == []
    store.finish_run(1, "crashed")  # The leader dies before its verdict: the claim stops reserving.
    picked = membership(ctx, 5, store)._candidates(rows(store), 3)
    assert [key[0] for key, _ in picked] == [4]  # 4 is free again; the dead leader is skipped.


def test_only_a_pass_verdict_for_the_exact_head_is_reused(tmp_path):
    store, ctx = setup(tmp_path, 1, 4)
    write(store, ready(store, 1, "1" * 40), ready(store, 4, "4" * 40),
          claim(store, "g", 1, [(1, "1" * 40), (4, "4" * 40)]),
          {"event": "verdict", "id": "g", "target": "main", "result": "pass", "culprits": []})
    group = membership(ctx, 4, store)
    group.at_turn("4" * 40)
    assert group.claim["id"] == "g" and group.size() == 2 and group.others() == [1]
    changed = membership(ctx, 4, store)
    changed.at_turn("f" * 40)  # A changed head never borrows the old verdict.
    assert changed.claim is None and changed.solo


def test_a_failed_verdict_sends_members_down_the_one_pr_path(tmp_path):
    store, ctx = setup(tmp_path, 1, 4)
    write(store, ready(store, 1, "1" * 40), ready(store, 4, "4" * 40),
          claim(store, "g", 1, [(1, "1" * 40), (4, "4" * 40)]),
          {"event": "verdict", "id": "g", "target": "main", "result": "fail", "culprits": []})
    group = membership(ctx, 4, store)
    group.at_turn("4" * 40)
    assert group.claim is None and group.solo and group.size() == 1


def test_only_the_last_live_member_holding_its_exact_head_reinstalls(tmp_path):
    store, ctx = setup(tmp_path, 1, 4, 5)
    write(store, *(ready(store, n, f"{n}" * 40) for n in (1, 4, 5)),
          claim(store, "g", 1, [(1, "1" * 40), (4, "4" * 40), (5, "5" * 40)]),
          {"event": "verdict", "id": "g", "target": "main", "result": "pass", "culprits": []})
    first = membership(ctx, 1, store)
    first.at_turn("1" * 40)
    assert first.reinstall_now() is False  # 4 and 5 still hold the verdict for their heads.
    write(store, ready(store, 4, "e" * 40))  # 4's head changed: it cannot consume the verdict.
    assert first.reinstall_now() is False  # 5 still can.
    store.finish_run(5, "crashed")
    assert first.reinstall_now() is True  # No later live member holds it: this merge reinstalls.
