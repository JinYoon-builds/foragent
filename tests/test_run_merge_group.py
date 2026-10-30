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


def git_repo(tmp_path):
    """A plumbing-only repository: commits are built without git commit or checkout."""
    import subprocess

    from foragent.git import GitRepo

    root = tmp_path / "repo"
    root.mkdir()

    def run(*args, stdin=None):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root,
                              check=True, capture_output=True, text=True, input=stdin).stdout.strip()

    run("init", "-q", "-b", "main")
    return GitRepo(root), run


def make_commit(run, files, parent=None, message="c"):
    """A commit whose tree holds exactly ``files`` (name -> content)."""
    entries = []
    for name, content in sorted(files.items()):
        blob = run("hash-object", "-w", "--stdin", stdin=content)
        entries.append(f"100644 blob {blob}\t{name}")
    tree = run("mktree", stdin="\n".join(entries) + "\n")
    return run("commit-tree", tree, *(["-p", parent] if parent else []), "-m", message)


def test_a_merge_is_identified_by_content_not_by_its_position(tmp_path):
    from foragent.run_merge_group import is_squash_of, squash_tree

    git, run = git_repo(tmp_path)
    base = make_commit(run, {"base.txt": "base\n"})
    head = make_commit(run, {"base.txt": "base\n", "member.md": "member\n"}, base, "member")
    # An outside source push lands between the member's base read and its squash merge.
    outside = make_commit(run, {"base.txt": "base\n", "outside.py": "VALUE = 1\n"}, base, "outside")
    squash = run("commit-tree", squash_tree(git, outside, head), "-p", outside, "-m", "squash")
    assert is_squash_of(git, squash, head)
    assert not is_squash_of(git, outside, head), "The first commit after the base is not proof"


def test_the_joint_tree_equals_sequential_squashes_even_when_a_head_reverts_an_earlier_member(tmp_path):
    from foragent.run_merge_group import squash_sequence

    git, run = git_repo(tmp_path)
    base = make_commit(run, {"base.txt": "base\n"})
    first = make_commit(run, {"base.txt": "base\n", "first.txt": "first\n"}, base, "first")
    # The second head contains the first member's commit, then reverts it.
    reverted = make_commit(run, {"base.txt": "base\n"}, first, "revert first")
    second = make_commit(run, {"base.txt": "base\n", "second.txt": "second\n"}, reverted, "second")
    attempt = SimpleNamespace(ctx=SimpleNamespace(git=git), log=tmp_path / "log")
    combined = squash_sequence(attempt, [{"n": 1, "head_sha": first}, {"n": 4, "head_sha": second}], base)
    files = set(run("ls-tree", "-r", "--name-only", combined).splitlines())
    # Two squashes publish both members' net changes; an ordinary merge would drop first.txt.
    assert files == {"base.txt", "first.txt", "second.txt"}


def test_a_member_without_a_baseline_never_counts_as_evidence_for_the_pinned_main(tmp_path, monkeypatch):
    from foragent import run_baseline
    from foragent.run_merge_group import _baseline_on

    captured = []
    monkeypatch.setattr(run_baseline, "_capture_acceptance_baseline",
                        lambda n, ctx, command, base: captured.append((command, base))
                        or SimpleNamespace(node_ids=("tests/acceptance/test_new.py::test_new",)))
    attempt = SimpleNamespace(n=1, ctx=SimpleNamespace(config=Config(test_command="pytest -q")),
                              log=tmp_path / "log")
    members = [{"baseline_base_sha": None}, {"baseline_base_sha": None}]  # Both started with no tests.
    assert _baseline_on(attempt, members, "b" * 40, tmp_path) == {"tests/acceptance/test_new.py::test_new"}
    assert captured == [("pytest -q", "b" * 40)]


def test_a_member_that_leaves_before_loading_its_claim_still_reinstalls_earlier_merges(tmp_path, monkeypatch):
    from foragent import runner

    store, ctx = setup(tmp_path, 1, 4)
    write(store, ready(store, 1, "1" * 40), ready(store, 4, "4" * 40),
          claim(store, "g", 1, [(1, "1" * 40), (4, "4" * 40)]),
          {"event": "verdict", "id": "g", "target": "main", "result": "pass", "culprits": []},
          {"n": 1, "event": "merged", "started_at": store.get_run(1).started_at, "target": "main",
           "group": "g", "head_sha": "1" * 40, "merged_sha": "m" * 40, "pr": 11})
    reinstalls = []
    monkeypatch.setattr(runner, "after_merge", lambda ctx, pr, base: reinstalls.append(pr))
    last = membership(ctx, 4, store)
    assert last.claim is None  # Its head changed while waiting: it never reached its turn.
    last.leave()
    assert reinstalls == [11], "The deferred reinstall of the group's merges must still run once"
    last.leave()
    assert reinstalls == [11]


def test_a_live_leaders_claim_gets_a_verdict_even_when_its_joint_check_raises(tmp_path, monkeypatch):
    import pytest

    from foragent import run_merge_group

    store, ctx = setup(tmp_path, 1, 4)
    write(store, ready(store, 1, "1" * 40), ready(store, 4, "4" * 40))
    ctx.git = SimpleNamespace(fetch=lambda remote, ref: None, ref_sha=lambda ref: "b" * 40)

    def broken(*args):
        raise RuntimeError("worktree vanished")

    monkeypatch.setattr(run_merge_group, "joint_check", broken)
    leader = membership(ctx, 1, store)
    leader.attempt.log = tmp_path / "log"
    with pytest.raises(RuntimeError):
        leader.at_turn("1" * 40)
    events = run_merge_group.rows(store)
    group = next(row for row in events if row.get("event") == "group")
    verdicts = [row for row in events if row.get("event") == "verdict" and row["id"] == group["id"]]
    assert [row["result"] for row in verdicts] == ["fail"], "No open claim may outlive its live leader's check"
