"""Shared offline helpers for unit tests."""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import shlex
import subprocess
import sys
from threading import RLock
from types import SimpleNamespace

from conftest import ACCEPTANCE, git_command

from helpers.publication import MERGED, PENDING, UNOWNED


MEMBERS = (5, 1, 4)  # Deliberately differs from issue-number order.
WORKFLOW = ".github/workflows/foragent-acceptance.yml"


def _tree_files(repo, ref):
    files = {}
    for entry in git_command(repo, "ls-tree", "-r", "-z", ref).split("\0"):
        if entry:
            metadata, name = entry.split("\t", 1)
            files[name] = metadata.split()[2]
    return files


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
                        before_merges=len(case.merges), tree=_tree_files(case.history.origin, head),
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
