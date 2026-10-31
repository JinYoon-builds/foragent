"""Group evidence (#1140): the joint verdict fails closed, and a follower may count an
earlier joint pass only when its head and the whole main interval still match."""

from contextlib import nullcontext
from dataclasses import replace
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from foragent import orch_events
from foragent import run_group_check as group
from foragent import run_group_evidence as evidence
from foragent import run_merge_turn as turn
from foragent.git import GitRepo
from foragent.run_baseline import AcceptanceBaseline
from foragent.run_checks import CheckResult
from foragent.tracker import TrackerError
from test_run_group_check import NOW, make_ctx

BASELINE = AcceptanceBaseline("pytest -q", ("tests/acceptance/test_a.py::t",))
IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@t"]


def ok(command="c"):
    return CheckResult(command, 0, False, "")


@pytest.mark.parametrize("error, baseline, results, expected", [
    (None, BASELINE, [ok(), ok()], "passed"),
    (None, BASELINE, [ok(), CheckResult("c", 1, False, "")], "failed"),
    (None, BASELINE, [ok(), CheckResult("c", None, False, "")], "void"),
    (None, BASELINE, [CheckResult("c", True, False, "")], "void"),
    (None, BASELINE, [CheckResult("c", 0, True, "")], "void"),
    (None, BASELINE, [CheckResult("c", 1, True, "")], "void"),
    (None, None, [ok()], "void"),
    (None, AcceptanceBaseline("pytest -q", ()), [ok()], "void"),
    ("OSError", BASELINE, [ok()], "void"),
])
def test_joint_verdict_passes_only_a_complete_check_that_exited_zero_in_time(error, baseline, results, expected):
    assert group.judge(error, baseline, results)[0] == expected


def joint_ctx(tmp_path, monkeypatch, check, baseline=BASELINE):
    from foragent import ci_mode, run_baseline, run_implementation, runner

    ctx = make_ctx(tmp_path, (5, 1))
    ctx.config = replace(ctx.config, test_command="pytest -q")
    ctx.repo_root = tmp_path
    monkeypatch.setattr(ci_mode, "enabled", lambda ctx, target: False)
    monkeypatch.setattr(run_implementation, "_detect_test_command_at", lambda git, sha: None)
    monkeypatch.setattr(run_baseline, "_capture_acceptance_baseline", lambda n, ctx, command, ref: baseline)
    monkeypatch.setattr(group, "_combine", lambda git, base, members: "combined")
    monkeypatch.setattr(group, "_test_slot", lambda n, ctx: nullcontext())
    monkeypatch.setattr(runner, "_check", check)
    ctx.git = SimpleNamespace(add_detached_worktree=lambda path, ref: None, remove_worktree=lambda path: None,
                              _call=lambda args: "")
    return ctx


def group_events(ctx):
    path = ctx.store.root / "orch-events.jsonl"
    return [event for event in map(json.loads, path.read_text().splitlines()) if event["kind"] == "group-check"]


def test_unreadable_joint_result_voids_the_group_without_ending_the_leader(tmp_path, monkeypatch):
    def unreadable(command, *args, **kwargs):
        raise OSError("result could not be read")

    ctx = joint_ctx(tmp_path, monkeypatch, unreadable)
    group.check(5, ctx, turn.seat(ctx, 5, "main"), "h5", "main-sha", set())
    [event] = group_events(ctx)
    assert (event["verdict"], event["passed"]) == ("void", False)
    assert "could not be read" in event["reason"]


def test_absent_baseline_runs_nothing_and_voids_the_group(tmp_path, monkeypatch):
    ran = []
    ctx = joint_ctx(tmp_path, monkeypatch, lambda command, *a, **k: ran.append(command) or ok(command), baseline=None)
    group.check(5, ctx, turn.seat(ctx, 5, "main"), "h5", "main-sha", set())
    [event] = group_events(ctx)
    assert ran == [] and event["verdict"] == "void"


# A real repository built with plumbing: group base B, then main commits.

def git(root: Path, *args: str, stdin: str | None = None) -> str:
    return subprocess.run(["git", "-C", str(root), *IDENTITY, *args], input=stdin, text=True,
                          capture_output=True, check=True).stdout.strip()


def tree(root: Path, files: dict[str, str]) -> str:
    entries = "".join(f"100644 blob {git(root, 'hash-object', '-w', '--stdin', stdin=text)}\t{name}\n"
                      for name, text in files.items())
    return git(root, "mktree", stdin=entries)


def commit(root: Path, files: dict[str, str], *parents: str, message: str = "x") -> str:
    return git(root, "commit-tree", tree(root, files), *[arg for p in parents for arg in ("-p", p)], "-m", message)


@pytest.fixture
def history(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    base = commit(root, {"a.py": "A\n"})
    after_1 = {"a.py": "A\n", "m1.py": "1\n"}
    head_1 = commit(root, after_1, base, message="member 1 head")
    ctx = make_ctx(tmp_path, (1, 4))
    ctx.git = GitRepo(root)
    monkeypatch.setattr(ctx.git, "fetch", lambda remote="origin", ref=None: None)
    heads = {1: head_1, 4: "h4"}
    ctx.tracker = SimpleNamespace(find_pr=lambda branch: int(branch.rsplit("-", 1)[1]),
                                  pr_head_sha=lambda pr: heads[pr])
    orch_events.append_event(ctx.store, NOW, 1, "group-check", "group check passed", base_sha=base,
                             members=[{"n": 1, "head_sha": head_1, "tree": tree(root, after_1)},
                                      {"n": 4, "head_sha": "h4", "tree": "t4"}],
                             verdict="passed", passed=True)

    def merge_1(*before: str) -> str:
        """Member 1's squash lands on main (after any ``before`` commits); it records itself."""
        tip = base
        for files in before:
            tip = commit(root, files, tip, message="outsider")
        squash = commit(root, {**after_1, **({"o.py": "O\n"} if before else {})}, tip, message="Member 1 (#1)")
        git(root, "update-ref", "refs/remotes/origin/main", squash)
        evidence.record_merge(1, ctx, "main", base, head_1)
        return squash

    return SimpleNamespace(root=root, base=base, after_1=after_1, ctx=ctx, heads=heads, head_1=head_1,
                           merge_1=merge_1)


def merge_events(ctx):
    path = ctx.store.root / "orch-events.jsonl"
    return [e for e in map(json.loads, path.read_text().splitlines()) if e["kind"] == "group-merge"]


def test_earlier_members_recorded_merge_leaves_the_joint_pass_usable(history):
    main = history.merge_1()
    [record] = merge_events(history.ctx)
    assert record["issue"] == 1 and record["commit"]["sha"] == main
    assert evidence.void_reason(history.ctx, 4, "h4", main) is None
    assert evidence.note(4, history.ctx, "h4", main) is None
    assert "Group evidence on" in history.ctx.store.log_path(4).read_text()


def test_identical_content_without_the_members_own_merge_record_is_not_provenance(history):
    # An outsider with the expected parent and tree, while #1 never merged.
    lookalike = commit(history.root, history.after_1, history.base, message="outsider lookalike")
    reason = evidence.void_reason(history.ctx, 4, "h4", lookalike)
    assert reason and "no recorded merge" in reason


def test_a_lookalike_beside_the_members_recorded_merge_is_still_not_that_merge(history):
    history.merge_1()
    lookalike = commit(history.root, history.after_1, history.base, message="outsider lookalike")
    assert "no recorded merge" in evidence.void_reason(history.ctx, 4, "h4", lookalike)


def test_outsider_racing_the_final_read_leaves_the_merge_unattributed(history):
    main = history.merge_1({"a.py": "A\n", "o.py": "O\n"})
    [record] = merge_events(history.ctx)
    assert record["commit"] is None and record["reason"]
    assert evidence.void_reason(history.ctx, 4, "h4", main)


def test_a_merge_with_no_later_group_member_records_nothing(history):
    history.ctx.store.root.joinpath("orch-events.jsonl").unlink()
    history.merge_1()
    assert not history.ctx.store.root.joinpath("orch-events.jsonl").exists()


def test_earlier_members_not_merged_yet_is_void(history):
    assert evidence.void_reason(history.ctx, 4, "h4", history.base)


def test_failed_or_void_group_is_never_spent(history):
    main = history.merge_1()
    orch_events.append_event(history.ctx.store, NOW, 1, "group-check", "group check void",
                             base_sha=history.base, verdict="void", passed=False,
                             members=[{"n": 1, "head_sha": history.head_1,
                                       "tree": tree(history.root, history.after_1)},
                                      {"n": 4, "head_sha": "h4"}])
    assert "void" in evidence.void_reason(history.ctx, 4, "h4", main)


def test_changed_head_cannot_spend_the_pass(history):
    main = history.merge_1()
    assert evidence.void_reason(history.ctx, 4, "other", main)
    history.heads[4] = "replaced"
    assert "changed" in evidence.void_reason(history.ctx, 4, "h4", main)


def test_unreadable_pr_head_is_void_but_a_rate_limit_propagates(history):
    main = history.merge_1()

    def fail(pr):
        raise TrackerError("boom")

    history.ctx.tracker.pr_head_sha = fail
    assert "unreadable" in evidence.void_reason(history.ctx, 4, "h4", main)

    def limited(pr):
        raise TrackerError("API rate limit exceeded")

    history.ctx.tracker.pr_head_sha = limited
    with pytest.raises(TrackerError):
        evidence.void_reason(history.ctx, 4, "h4", main)


@pytest.mark.parametrize("shape", ["outsider-before", "outsider-after", "reverted-outsider",
                                   "wrong-tree", "unrelated-main"])
def test_any_other_commit_in_the_base_interval_voids_the_pass(history, shape):
    root, base, after_1 = history.root, history.base, history.after_1
    if shape == "outsider-before":
        # An outsider lands inside the member's merge call: the member's squash sits on it.
        outsider = commit(root, {**{"a.py": "A\n"}, "o.py": "O\n"}, base)
        main = commit(root, {**after_1, "o.py": "O\n"}, outsider)
    elif shape == "outsider-after":
        main = commit(root, {**after_1, "o.py": "O\n"}, commit(root, after_1, base))
    elif shape == "reverted-outsider":
        pushed = commit(root, {"a.py": "A\n", "o.py": "O\n"}, base)
        reverted = commit(root, {"a.py": "A\n"}, pushed)
        main = commit(root, after_1, reverted)  # same net tree as the clean interval
    elif shape == "wrong-tree":
        main = commit(root, {**after_1, "m1.py": "other\n"}, base)
    else:
        main = commit(root, after_1)
    assert evidence.void_reason(history.ctx, 4, "h4", main)


def record_at(history, tip):
    git(history.root, "update-ref", "refs/remotes/origin/main", tip)
    evidence.record_merge(1, history.ctx, "main", history.base, history.head_1)
    return merge_events(history.ctx)[-1]


def test_rewritten_main_whose_first_commit_has_another_parent_is_unattributed(history):
    other_root = commit(history.root, {"z.py": "Z\n"}, message="rewritten root")
    tip = commit(history.root, history.after_1, other_root, message="Member 1 (#1)")
    assert record_at(history, tip)["commit"] is None


def test_a_second_commit_with_the_same_tree_makes_the_merge_ambiguous(history):
    squash = commit(history.root, history.after_1, history.base, message="Member 1 (#1)")
    tip = commit(history.root, history.after_1, squash, message="empty outsider")
    assert record_at(history, tip)["commit"] is None


def test_a_commit_built_on_an_older_tip_is_not_this_merge(history):
    read_tip = commit(history.root, {"a.py": "A\n"}, history.base, message="tip read under the lock")
    stale = commit(history.root, history.after_1, history.base, message="Member 1 (#1)")
    git(history.root, "update-ref", "refs/remotes/origin/main", stale)
    evidence.record_merge(1, history.ctx, "main", read_tip, history.head_1)
    assert merge_events(history.ctx)[-1]["commit"] is None


def test_refused_credential_reading_evidence_ends_the_run_instead_of_voiding(history, monkeypatch):
    """#1208: a refused read is not void evidence; it propagates to auth-failed."""
    from foragent.git import GitError

    main = history.merge_1()

    def refused_head(pr):
        raise TrackerError("gh: Bad credentials (HTTP 401)")
    history.ctx.tracker.pr_head_sha = refused_head
    with pytest.raises(TrackerError):
        evidence.void_reason(history.ctx, 4, "h4", main)
    history.ctx.tracker.pr_head_sha = lambda pr: history.heads[pr]
    real_call = history.ctx.git._call

    def refused_log(args):
        if args[:1] == ["log"]:
            raise GitError("fatal: Could not read from remote repository.")
        return real_call(args)
    monkeypatch.setattr(history.ctx.git, "_call", refused_log)
    with pytest.raises(GitError):
        evidence.void_reason(history.ctx, 4, "h4", main)


def test_refused_provenance_fetch_after_the_merge_keeps_merged_with_its_reason(history, monkeypatch):
    from foragent.git import GitError

    def refuse(remote="origin", ref=None):
        raise GitError("git@github.com: Permission denied (publickey).")
    monkeypatch.setattr(history.ctx.git, "fetch", refuse)
    history.ctx.store.record_merge(1, NOW)  # the publication records the merge first
    evidence.record_merge(1, history.ctx, "main", history.base, history.head_1)
    run = history.ctx.store.get_run(1)
    assert run.outcome == "merged" and "publickey" in run.detail
    path = history.ctx.store.root / "orch-events.jsonl"
    kinds = [e["kind"] for e in map(json.loads, path.read_text().splitlines()) if e.get("issue") == 1]
    assert kinds.count("auth-failed") == 1 and merge_events(history.ctx)[-1]["commit"] is None
