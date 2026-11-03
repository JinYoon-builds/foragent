"""Locked acceptance for ci-group-evidence; do not edit during implementation.

Drive runner.run_issue through the existing FIFO fixture, with real Git history
and a fake GitHub transport. No group, comparison, refresh or merge decision is
replaced. A CI round is a changed published branch head, not an evidence poll.
Initial PR CI has already run when all three members become ready; the counts
below cover the group turn and its subsequent merges.

Break-it: the CI-mode early return, local-only group checks, a partial combined
tree, duplicate CI publications, follower refreshes, trusting failed/void CI,
out-of-order/duplicate merges and cross-owner writes all violate assertions.
"""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import shlex
import subprocess
import sys
from threading import RLock
from types import SimpleNamespace

import pytest
import yaml

from conftest import ACCEPTANCE, git_command
from foragent.config import load_config
from test_base_move_reuse import MERGED, PENDING, UNOWNED
from test_merge_group import (  # noqa: F401
    contend, group_case, isolated_home, offline_acceptance, prepare, publish_case,
)


MEMBERS = (5, 1, 4)  # Deliberately differs from issue-number order.
WORKFLOW = ".github/workflows/foragent-acceptance.yml"


def tree_files(repo, ref):
    files = {}
    for entry in git_command(repo, "ls-tree", "-r", "-z", ref).split("\0"):
        if entry:
            metadata, name = entry.split("\t", 1)
            files[name] = metadata.split()[2]
    return files


def group_records(case):
    path = case.store.root / "orch-events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [row for row in rows if row.get("kind") == "group-check"]


class GitHubCI:
    """Offline external transport: publish refs, then serve immutable CI shards.

    Updating a remote ref starts one simulated Actions round. Reports are pinned
    to the main and head SHAs at publication, so an old report cannot silently
    become evidence on a newer main. Reads never start a round. Git transfers
    use fetch/update-ref, keeping the test's forbidden Git commands unexecuted.
    """

    def __init__(self, case, monkeypatch, result):
        self.case, self.result = case, result
        self.rounds, self.reads, self.pr_writes, self.deletions = [], [], [], []
        self.by_head = {}
        self.lock = RLock()
        self.waiting_for_group = False
        transport = case.git._run

        def git_transport(argv):
            root, args = case.root, list(argv)
            if args[:1] == ["-C"]:
                root, args = Path(args[1]), args[2:]
            if args[:1] != ["push"]:
                return transport(argv)
            # Only the external ref update is fake; the production code decides
            # which commit/branch to publish and when to publish or remove it.
            with self.lock:
                spec = args[-1]
                deleting = "--delete" in args or spec.startswith(":")
                if deleting:
                    branch = spec.removeprefix(":").removeprefix("refs/heads/")
                    git_command(case.history.origin, "update-ref", "-d", f"refs/heads/{branch}")
                    self.deletions.append((self.actor, branch))
                    return ""
                source, destination = spec.split(":", 1) if ":" in spec else (spec, spec)
                branch = destination.removeprefix("refs/heads/")
                head = git_command(root, "rev-parse", source)
                old = subprocess.run(
                    ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
                    cwd=case.history.origin, text=True, capture_output=True, check=False,
                )
                git_command(case.history.origin, "fetch", str(root),
                            f"+{source}:refs/heads/{branch}")
                if old.returncode or old.stdout.strip() != head:
                    row = SimpleNamespace(
                        head=head, base=git_command(case.history.origin, "rev-parse", "main"),
                        branch=branch, actor=self.actor, ready=case.armed,
                        before_merges=len(case.merges), tree=tree_files(case.history.origin, head),
                        run_id=8000 + len(self.by_head),
                    )
                    row.present = frozenset(n for n in MEMBERS if f"src/member_{n}.py" in row.tree)
                    row.group = branch not in {f"foragent/issue-{n}" for n in MEMBERS}
                    row.reports = self.reports(row)
                    self.by_head[head] = row
                    if row.ready:
                        self.rounds.append(row)
                pr = case.tracker.find_pr(branch)
                if pr is None and branch == f"foragent/issue-{self.actor}":
                    pr = max(case.tracker.issues) + 1  # Existing fixture's pre-create PR identity.
                if pr is not None:
                    case.tracker.set_pr_head_sha(pr, head)
                return ""

        def observe_pr(method):
            original = getattr(case.tracker, method)

            def write(*args, **kwargs):
                pr = original(*args, **kwargs)
                number = pr if method == "create_pr" else args[0]
                item = next(item for item in case.tracker.created_prs if item.number == number)
                self.pr_writes.append((self.actor, item.head, method))
                if method == "create_pr":
                    head = git_command(case.history.origin, "rev-parse", f"refs/heads/{item.head}")
                    case.tracker.set_pr_head_sha(number, head)
                elif method == "close_pr":
                    # GhTracker.close_pr uses --delete-branch. FakeTracker
                    # records that deletion; mirror it in the offline origin.
                    git_command(case.history.origin, "update-ref", "-d", f"refs/heads/{item.head}")
                    self.deletions.append((self.actor, item.head))
                return pr

            monkeypatch.setattr(case.tracker, method, write)

        monkeypatch.setattr(case.git, "_run", git_transport)
        monkeypatch.setattr(case.tracker, "pr_acceptance_reports", self.acceptance_reports)
        for method in ("create_pr", "update_pr", "close_pr"):
            observe_pr(method)
        sleep = case.ctx.sleep

        def wait(seconds):
            # Advance only the leader's CI wait, never a follower's FIFO wait.
            if self.waiting_for_group and self.actor == MEMBERS[0]:
                case.clock[0] += timedelta(seconds=seconds)
            sleep(seconds)

        case.ctx.sleep = wait

    @property
    def actor(self):
        return getattr(self.case.worker, "number", None)

    def reports(self, row):
        case = self.case
        combined = row.group
        if combined and self.result == "timeout":
            return []
        excluded = sorted([ACCEPTANCE, PENDING])
        command = (f"PYTEST_ADDOPTS= {shlex.quote(sys.executable)} -m pytest "
                   "-o addopts= --rootdir=. --maxfail=0 -p no:cacheprovider")
        result = []
        for index, node in enumerate((f"{MERGED}::test_merged", f"{UNOWNED}::test_unowned")):
            passing = [] if combined and self.result == "failed" and index == 0 else [node]
            report = {
                "schema_version": 99 if combined and self.result == "void" else 1,
                "repository": case.ctx.repo, "base_sha": row.base, "head_sha": row.head,
                "excluded": excluded, "shard_count": 2, "shard_index": index,
                "duration_seconds": 1.0,
                "scope": {"acceptance_dir": case.ctx.config.acceptance_dir, "command": command,
                          "marker": "not local_only", "excluded": excluded, "shard_count": 2},
                "base": {"complete": True, "collected_ids": [node], "passing_ids": [node]},
                "head": {"complete": True, "collected_ids": [node], "passing_ids": passing},
            }
            result.append({
                "run": {"id": row.run_id, "head_sha": row.head, "status": "completed",
                        "conclusion": "success", "event": "pull_request", "path": WORKFLOW,
                        "repository": {"full_name": case.ctx.repo},
                        "head_repository": {"full_name": case.ctx.repo}},
                "artifact": {"id": row.run_id * 10 + index, "name": f"acceptance-{index}",
                             "expired": False, "workflow_run": {"id": row.run_id, "head_sha": row.head}},
                "report": report,
            })
        return result

    def acceptance_reports(self, head):
        with self.lock:
            assert head in self.by_head, "CI evidence requires a published commit"
            row = self.by_head[head]
            self.reads.append((self.actor, head, len(self.case.merges)))
            self.waiting_for_group = row.group and self.result == "timeout"
            return deepcopy(row.reports)


def configure(case, monkeypatch, *, maximum=3, result="passed"):
    prepare(case, MEMBERS)
    command = f"{shlex.quote(sys.executable)} -m pytest -q"
    settings = {"repo": "owner/repo", "review": "fast", "checks": "ci", "base_branch": "main",
                "merge_group_max": maximum, "ci_shards": 2, "test_command": command,
                "skill_libraries": [], "approvers": ["human"]}
    # Put the configured mode on main too, so all real worktrees inherit it.
    case.history.update({".foragent.yml": yaml.safe_dump(settings), WORKFLOW:
        "name: offline-ci\non: [pull_request]\njobs:\n  acceptance:\n"
        "    runs-on: ubuntu-latest\n    steps: []\n"})
    git_command(case.root, "fetch", "-q", "origin")
    git_command(case.root, "merge", "-q", "--ff-only", "origin/main")
    case.base_sha = case.history.tip
    config = load_config(case.root, global_path=case.root / "absent.yml")
    assert (config.checks, config.base_branch, config.merge_group_max) == ("ci", "main", maximum)
    case.ctx.config = replace(case.ctx.config, checks=config.checks, base_branch=config.base_branch,
                              merge_group_max=config.merge_group_max, ci_shards=config.ci_shards,
                              test_command=config.test_command, checks_fallback=None)
    ci = GitHubCI(case, monkeypatch, result)
    return ci


def assert_finished(case):
    assert [row.issue for row in case.merges] == list(MEMBERS), "Each owner merges once, in FIFO order"
    assert all(case.outcomes[n] == "merged" for n in MEMBERS), case.outcomes
    assert all(case.tracker.get_issue(n).state == "closed" for n in MEMBERS)
    assert all(actor == n for actor, n in case.record_writes), "Members write only their own run records"
    assert all(actor == n for actor, n in case.closes if n in MEMBERS), "Members close only their own issues"
    assert all(actor == n for actor, n, _ in case.board_writes), "Members update only their own board records"


def assert_combined_round(case, ci):
    records = group_records(case)
    assert records, "checks: ci skipped the group check: no group-check record was written"
    record = next((row for row in records if [m["n"] for m in row.get("members", [])] == list(MEMBERS)), None)
    assert record is not None, "The group record must cover the three ready heads in FIFO order"
    assert record["issue"] == MEMBERS[0], "Only the leader writes the group result"
    assert record["base_sha"] == case.group_base
    ready = {row["n"]: row["head_sha"] for _, row in case.appends if row["event"] == "ready"}
    assert {m["n"]: m["head_sha"] for m in record["members"]} == ready
    combined = [row for row in ci.rounds if row.group]
    assert len(combined) == 1, "Run GitHub CI exactly once on the combined commit"
    joint, = combined
    assert joint.present == frozenset(MEMBERS), "The CI commit must contain all three members"
    assert joint.base == case.group_base and joint.before_merges == 0
    assert joint.head not in ready.values(), "CI must measure the combined commit, not a member's head"
    git_command(case.history.origin, "merge-base", "--is-ancestor", case.group_base, joint.head)
    assert git_command(case.history.origin, "show", f"{joint.head}:src/epoch.py") == "EPOCH = 0"
    assert any(head == joint.head and merges == 0 for _, head, merges in ci.reads), (
        "Import the combined commit's CI evidence before merging any member"
    )
    assert joint.actor == MEMBERS[0], "Only the leader publishes the group branch"
    group_writes = [(actor, branch) for actor, branch, _ in ci.pr_writes
                    if branch not in {f"foragent/issue-{n}" for n in MEMBERS}]
    assert all(actor == MEMBERS[0] for actor, _ in group_writes)
    assert all(actor == MEMBERS[0] for actor, branch in ci.deletions if branch == joint.branch)
    refs = git_command(case.history.origin, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    assert joint.branch not in refs.splitlines(), "Remove the temporary group branch after the group run"
    assert all(pr.head != joint.branch for pr in case.tracker.list_prs("open")), "Close any temporary group PR"
    return record, joint, ready


def test_ci_mode_checks_the_combined_commit_once_and_reuses_it_for_three_merges(group_case, monkeypatch):
    case = group_case
    ci = configure(case, monkeypatch)
    contend(case)
    record, joint, ready = assert_combined_round(case, ci)
    assert record["verdict"] == "passed"
    assert len(ci.rounds) == 1, "Covered predecessor merges must not start per-member CI rounds"
    assert_finished(case)
    assert all(row.head == ready[row.issue] for row in case.merges), "Merge the original covered PR heads"
    assert tree_files(case.history.origin, "main") == joint.tree, "Publish exactly the tree checked by group CI"


@pytest.mark.parametrize("result", ["failed", "void", "timeout"])
def test_unusable_combined_ci_falls_back_to_individual_evidence(group_case, monkeypatch, result):
    case = group_case
    ci = configure(case, monkeypatch, result=result)
    contend(case)
    record, joint, _ = assert_combined_round(case, ci)
    assert record["verdict"] == ("failed" if result == "failed" else "void")
    assert_finished(case)
    for merge in case.merges:
        assert merge.head != joint.head
        evidence = ci.by_head[merge.head]
        assert evidence.base == merge.base, "A failed/void group requires each member's evidence on current main"
        assert any(actor == merge.issue and head == merge.head for actor, head, _ in ci.reads), (
            "Every fallback member imports its own CI evidence"
        )
    assert any(row.actor == MEMBERS[1] and row.present != frozenset(MEMBERS) for row in ci.rounds)


def test_max_one_keeps_individual_ci_and_never_publishes_a_group(group_case, monkeypatch):
    case = group_case
    ci = configure(case, monkeypatch, maximum=1)
    contend(case)
    assert_finished(case)
    assert group_records(case) == []
    assert not any(row.present == frozenset(MEMBERS) for row in ci.rounds if row.before_merges == 0)
    assert all(branch in {f"foragent/issue-{n}" for n in MEMBERS} for _, branch, _ in ci.pr_writes)
    assert len(ci.rounds) == 2, "Today each later member refreshes its head and runs CI on the new main"
    for merge in case.merges:
        assert ci.by_head[merge.head].base == merge.base
        assert any(actor == merge.issue and head == merge.head for actor, head, _ in ci.reads)
