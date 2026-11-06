"""Shared offline helpers for unit tests."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import shlex
import sys

import pytest

from conftest import ACCEPTANCE, git_command
from foragent.git import GitRepo
from foragent.issue import body_hash

from helpers.publication import MERGED, PENDING, UNOWNED, publish_case


@pytest.fixture
def neutral_case(publish_case, monkeypatch):
    case = publish_case
    case.ctx.config = replace(case.ctx.config, checks="ci", checks_fallback=None,
                              ci_shards=1, merge_group_max=1)
    case.checked = case.root / "checked-heads.txt"
    approval = case.store.load_approval(1)
    body = approval.body + f"\n```sh\ngit rev-parse HEAD >> {shlex.quote(str(case.checked))}\n```\n"
    case.tracker.update_issue(1, title=case.tracker.get_issue(1).title, body=body)
    case.store.save_approval(1, body=body, body_sha=body_hash(body),
                            acceptance_blobs=approval.acceptance_blobs,
                            approved_by="human", approved_at=case.clock[0].isoformat())
    case.publications, case.reports, case.polls, case.sleeps, case.landed = [], {}, [], [], []
    case.held = False
    case.locks = 0
    case.on_poll = lambda: None
    case.on_lock = lambda: None
    case.status = lambda head: "pass"
    case.original_head = None

    def review(kwargs):
        case.reviewed_tree = git_command(kwargs["worktree"], "write-tree")
        return "VERDICT: PASS"

    case.reviewer.steps = [review]

    # Observe a public Git operation. The shared transport imports objects and
    # transfers refs locally; no git commit/push/checkout/reset/stash executes.
    push = GitRepo.push

    def publish(git, branch, *, lease=None):
        head = git.head_sha()
        base = git_command(case.history.origin, "rev-parse", "main")
        push(git, branch, lease=lease)
        if head not in case.reports:
            case.publications.append(head)
            case.original_head = case.original_head or head
            excluded = sorted([ACCEPTANCE, PENDING])
            nodes = [f"{MERGED}::test_merged", f"{UNOWNED}::test_unowned"]
            suite = {"complete": True, "collected_ids": nodes, "passing_ids": nodes}
            command = (f"PYTEST_ADDOPTS= {shlex.quote(sys.executable)} -m pytest "
                       "-p no:cacheprovider -o addopts= --rootdir=. --maxfail=0 -p no:cacheprovider")
            run_id = 9000 + len(case.publications)
            case.reports[head] = [{
                "run": {"id": run_id, "head_sha": head, "status": "completed",
                        "event": "pull_request", "path": ".github/workflows/foragent-acceptance.yml",
                        "repository": {"full_name": case.ctx.repo},
                        "head_repository": {"full_name": case.ctx.repo}},
                "artifact": {"id": run_id, "name": "acceptance-0", "expired": False,
                             "workflow_run": {"id": run_id, "head_sha": head}},
                "report": {"schema_version": 1, "repository": case.ctx.repo,
                           "base_sha": base, "head_sha": head, "excluded": excluded,
                           "shard_index": 0, "shard_count": 1, "duration_seconds": 1,
                           "scope": {"acceptance_dir": case.ctx.config.acceptance_dir, "command": command,
                                     "marker": "not local_only", "excluded": excluded, "shard_count": 1},
                           "base": suite, "head": suite},
            }]

    def reports(head):
        return deepcopy(case.reports[head])  # Never rewrite a report after main moves.

    def poll(pr):
        head = case.tracker.pr_head_sha(pr)
        status = case.status(head)
        case.polls.append((head, status, case.held))
        case.on_poll()
        return status

    lock = case.store.merge_lock

    @contextmanager
    def final_lock():
        case.locks += 1
        case.on_lock()
        with lock():
            case.held = True
            try:
                yield
            finally:
                case.held = False

    merge = case.tracker.merge_pr

    def land(pr, *, expected_head_sha, method="squash", **kwargs):
        assert case.held, "The main write must hold the final merge lock"
        assert case.polls[-1] == (expected_head_sha, "pass", True), "Re-read CI inside the final lock"
        assert "approved" in case.tracker.get_issue(1).labels
        assert case.tracker.pr_head_sha(pr) == expected_head_sha
        base = case.history.tip
        # GitHub squashes the unchanged head onto current main. Preserve all
        # base files, including neutral files absent from the PR head.
        common = git_command(case.history.origin, "merge-base", base, expected_head_sha)
        paths = git_command(case.history.origin, "diff", "--name-only", common, expected_head_sha).splitlines()
        files = {path: git_command(case.history.origin, "show", f"{expected_head_sha}:{path}") + "\n"
                 for path in paths}
        case.history.update(files)
        case.landed.append((base, expected_head_sha, case.history.tip))
        return merge(pr, expected_head_sha=expected_head_sha, method=method, **kwargs)

    sleep = case.ctx.sleep

    def wait(seconds):
        assert not case.held, "CI waiting must release the final lock"
        case.sleeps.append(seconds)
        sleep(seconds)

    monkeypatch.setattr(GitRepo, "push", publish)
    monkeypatch.setattr(case.tracker, "pr_acceptance_reports", reports)
    monkeypatch.setattr(case.tracker, "pr_checks", poll)
    monkeypatch.setattr(case.store, "merge_lock", final_lock)
    monkeypatch.setattr(case.tracker, "merge_pr", land)
    case.ctx.sleep = wait
    return case
