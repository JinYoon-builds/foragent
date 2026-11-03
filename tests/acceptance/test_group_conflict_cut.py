"""Locked acceptance for group-conflict-cut; do not edit during implementation.

Reject voiding a clean prefix, skipping a conflicting member to admit later
heads, checking conflict-marker trees, singleton group results, changing the
auto limit, and losing exact CI evidence or its exclusions trailer on a cut.
Real runner.run_issue, Git merge-tree and FIFO publication make the decisions;
the existing offline tracker, executor and GitHubCI fakes supply external I/O.
"""

from collections import Counter
from dataclasses import replace
import json
import shlex
import subprocess

import pytest

from conftest import git_command
from foragent import run_group_check
from foragent.ci_workflow import EXCLUSIONS_TRAILER
import test_ci_group_evidence as ci_helpers
from test_merge_group import (  # noqa: F401
    assert_suite, contend, group_case, group_rows, isolated_home,
    offline_acceptance, prepare, publish_case,
)


CONFLICT = "src/group_conflict.py"


def configure(case, monkeypatch, numbers, mode):
    """Reuse public-path group fixtures, including CI's real comparison consumer."""
    if mode == "ci":
        # The existing transport derives its member-file observations from this
        # fixture constant; production receives only normal config and PRs.
        monkeypatch.setattr(ci_helpers, "MEMBERS", numbers)
        ci = ci_helpers.configure(case, monkeypatch, maximum="auto")
    else:
        prepare(case, numbers)
        ci = None
    # A real conflicting PR keeps today's ordinary conflict outcome. This
    # task does not add a model resolution or rebuild to make it merge.
    case.ctx.config = replace(case.ctx.config, conflict_retries=0)
    case.merged_trees = {}

    def observe_merge(n):
        case.merged_trees[n] = git_command(case.history.origin, "rev-parse", "main^{tree}")

    case.on_merged = observe_merge
    return ci


def conflicting_heads(case, index):
    """Real add/add conflict: each head passes alone; their ordered union cannot."""
    implement = case.implementer.steps[0]
    writers = {case.members[0], case.members[index]}

    def implement_conflict(kwargs):
        implement(kwargs)
        n = case.worker.number
        if n in writers:
            (kwargs["worktree"] / CONFLICT).write_text(f"VALUE = {n}\n")

    case.implementer.steps = [implement_conflict] * len(case.members)


def ready_heads(case):
    return {row["n"]: row["head_sha"] for _, row in case.appends if row["event"] == "ready"}


def records(case):
    return ci_helpers.group_records(case)


def assert_ordinary_conflict(case, n):
    """Reject borrowing prefix evidence for an excluded, actually conflicting head."""
    assert case.outcomes[n] == "retries-exceeded", case.store.log_path(n).read_text()
    assert case.tracker.get_issue(n).state == "open"
    assert n not in {row.issue for row in case.merges}
    own = [row for row in case.probes if row.actor == n and "tools/group_probe.py" in row.command]
    assert own and all(row.worktree == case.store.worktree_path(n) for row in own)
    assert CONFLICT in case.store.get_run(n).merge_conflicts


@pytest.mark.parametrize("mode", ["local", "ci"])
def test_third_conflicting_head_preserves_one_joint_check_and_two_exact_merges(
    group_case, monkeypatch, mode,
):
    """Reject whole-group void, dropping the clean prefix, skipping the cut, or shrinking auto."""
    case = group_case
    numbers, prefix = (5, 1, 4, 7), (5, 1)
    ci = configure(case, monkeypatch, numbers, mode)
    conflicting_heads(case, 2)
    config_before = (case.root / ".foragent.yml").read_bytes()
    assert run_group_check.group_limit(case.store, case.ctx.config) == (4, "auto")
    contend(case)

    # Establish the actual conflict and ordinary suffix behavior before the
    # new prefix assertion, so a red run still validates the scenario itself.
    assert_ordinary_conflict(case, 4)
    assert [row.issue for row in case.merges] == [5, 1, 7]
    assert all(case.outcomes[n] == "merged" for n in (5, 1, 7))
    events = records(case)
    assert events and all(row["verdict"] == "passed" for row in events), (
        "A real merge-tree conflict must cut membership, not void or fail a joint check",
        events,
    )
    assert [[m["n"] for m in row["members"]] for row in events] == [list(prefix)]
    record, = events
    ready = ready_heads(case)
    assert record["issue"] == prefix[0] and record["base_sha"] == case.group_base
    assert {m["n"]: m["head_sha"] for m in record["members"]} == {n: ready[n] for n in prefix}
    assert all(m["tree"] == case.merged_trees[m["n"]] for m in record["members"]), (
        "Each prefix member must merge exactly its checked intermediate tree"
    )
    assert all(row.head == ready[row.issue] for row in case.merges if row.issue in prefix), (
        "Both prefix owners must spend the joint pass on their original ready heads"
    )
    assert all(actor == n for actor, n in case.record_writes)
    assert all(actor == n for actor, n in case.closes)
    assert all(actor == n for actor, n, _ in case.board_writes)
    assert run_group_check.group_limit(case.store, case.ctx.config) == (4, "auto"), (
        "The cut is not a failed check; a two-member pass cannot grow the four-member limit either"
    )
    assert (case.root / ".foragent.yml").read_bytes() == config_before

    if mode == "local":
        assert_suite(group_rows(case, prefix), prefix)
        done = [row for row in case.probes if row.armed and "tools/group_probe.py" in row.command]
        prefix_checks = [row for row in done if int(shlex.split(row.command)[-1]) in prefix]
        assert Counter(int(shlex.split(row.command)[-1]) for row in prefix_checks) == Counter(prefix)
        assert all(row.actor == prefix[0] and row.before_merges == 0 for row in prefix_checks)
        last_checks = [row for row in done if row.actor == 7]
        assert last_checks and all(row.worktree == case.store.worktree_path(7) for row in last_checks)
        assert all(row.before_merges == 2 for row in last_checks), "The suffix uses its own ordinary turn"
    else:
        assert record["mode"] == "ci" and record["reports_sha256"] and record["run_ids"]
        joint, = [row for row in ci.rounds if row.group]
        assert joint.actor == prefix[0] and joint.present == frozenset(prefix)
        assert joint.base == case.group_base and joint.before_merges == 0
        assert joint.head == record["combined_sha"] and joint.head not in ready.values()
        assert git_command(case.history.origin, "rev-parse", f"{joint.head}^{{tree}}") == case.merged_trees[1]
        message = git_command(case.history.origin, "show", "-s", "--format=%B", joint.head)
        trailer = next((line for line in message.splitlines() if line.startswith(f"{EXCLUSIONS_TRAILER}:")), None)
        assert trailer is not None, "Cutting before the original last member must retain CI exclusions"
        assert json.loads(trailer.split(":", 1)[1]) == record["scope"]["excluded"]
        assert all(any(actor == n and head == joint.head for actor, head, _ in ci.reads) for n in prefix)
        assert not any(actor in numbers[2:] and head == joint.head for actor, head, _ in ci.reads)
        assert not any(row.actor in prefix and not row.group for row in ci.rounds)
        last_merge, = [row for row in case.merges if row.issue == 7]
        assert ci.by_head[last_merge.head].base == last_merge.base
        assert any(actor == 7 and head == last_merge.head for actor, head, _ in ci.reads)
        assert joint.branch not in git_command(case.history.origin, "for-each-ref", "--format=%(refname:short)",
                                               "refs/heads").splitlines()
        assert not any(pr.head == joint.branch for pr in case.tracker.list_prs("open"))


@pytest.mark.parametrize("mode", ["local", "ci"])
def test_second_conflicting_head_leaves_leader_solo_without_a_group_verdict(
    group_case, monkeypatch, mode,
):
    """Reject a singleton group, conflict-as-void, conflict-as-failure, or a changed leader head."""
    case = group_case
    ci = configure(case, monkeypatch, (5, 1), mode)
    conflicting_heads(case, 1)
    contend(case)
    assert_ordinary_conflict(case, 1)
    ready = ready_heads(case)
    assert [(row.issue, row.head) for row in case.merges] == [(5, ready[5])]
    assert case.outcomes[5] == "merged"
    assert records(case) == [], "A prefix containing only the leader is no group, exactly like an empty group"
    assert run_group_check.group_limit(case.store, case.ctx.config) == (4, "auto")
    if mode == "local":
        own = [row for row in case.probes if row.actor == 5 and "tools/group_probe.py" in row.command]
        assert len(own) == 1 and own[0].worktree == case.store.worktree_path(5)
    else:
        assert not any(row.group for row in ci.rounds)
        assert any(actor == 5 and head == ready[5] for actor, head, _ in ci.reads)


def infrastructure_failure(case, monkeypatch, failure):
    """Inject only Git I/O, preserving real combine, verdict, reuse and merge decisions."""
    transport = case.git._run
    injected = []
    missing = [False]

    def unavailable(argv):
        args = list(argv)
        if args[:1] == ["-C"]:
            return transport(argv)
        if case.armed and not case.merges and case.worker.number == case.members[0]:
            second = ready_heads(case)[case.members[1]]
            if failure == "fetch" and args == ["cat-file", "-e", f"{second}^{{commit}}"]:
                missing[0] = True
                raise subprocess.CalledProcessError(128, ["git", *args], stderr="fatal: unreadable object")
            if failure == "fetch" and missing[0] and args[:1] == ["fetch"]:
                injected.append(failure)
                missing[0] = False
                raise subprocess.CalledProcessError(128, ["git", *args], stderr="fatal: offline fetch failure")
            if failure != "fetch" and args[:2] == ["merge-tree", "--write-tree"] and args[-1] == second:
                injected.append(failure)
                if failure == "not-runnable":
                    raise FileNotFoundError("offline git executable unavailable")
                # Git's exit status, rather than an English word in diagnostics,
                # distinguishes infrastructure from a real merge-tree conflict.
                raise subprocess.CalledProcessError(
                    128, ["git", *args], stderr="fatal: unreadable object named CONFLICT",
                )
        return transport(argv)

    monkeypatch.setattr(case.git, "_run", unavailable)
    return injected


@pytest.mark.parametrize("mode", ["local", "ci"])
@pytest.mark.parametrize("failure", ["unreadable-object", "not-runnable", "fetch"])
def test_combine_infrastructure_failure_still_voids_and_falls_back(
    group_case, monkeypatch, mode, failure,
):
    """Reject swallowing all GitError/OSError failures as a successful cut or no-group return."""
    case = group_case
    ci = configure(case, monkeypatch, (5, 1), mode)
    injected = infrastructure_failure(case, monkeypatch, failure)
    contend(case)
    assert injected == [failure]
    record, = records(case)
    assert record["verdict"] == "void" and record["passed"] is False and record["reason"]
    assert [m["n"] for m in record["members"]] == [5, 1]
    assert [row.issue for row in case.merges] == [5, 1]
    assert all(case.outcomes[n] == "merged" for n in (5, 1))
    assert run_group_check.group_limit(case.store, case.ctx.config) == (4, "auto")
    if mode == "local":
        own = [row for row in case.probes if row.actor == 1 and row.armed
               and "tools/group_probe.py" in row.command]
        assert own and all(row.worktree == case.store.worktree_path(1) for row in own)
    else:
        assert record["mode"] == "ci" and not any(row.group for row in ci.rounds)
        for merge in case.merges:
            assert ci.by_head[merge.head].base == merge.base
            assert any(actor == merge.issue and head == merge.head for actor, head, _ in ci.reads)
