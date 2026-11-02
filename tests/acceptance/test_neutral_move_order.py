"""Locked public outcomes for neutral-move. Offline GitHub and model fakes only."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import shlex
import sys

import pytest

from conftest import ACCEPTANCE, git_command
from foragent import runner
from foragent.git import GitRepo
from foragent.issue import body_hash
from test_base_move_reuse import MERGED, PENDING, UNOWNED, locked_test, publish_case  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401


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


def schedule_move(case, files, when):
    moved = []

    def move():
        if not moved:
            case.history.update(files)
            moved.append(case.history.tip)

    if when == "before-push":
        review_head = case.reviewer.steps[0]
        def review(kwargs):
            review_head(kwargs)
            move()
            return "VERDICT: PASS"
        case.reviewer.steps = [review]
    elif when == "after-ci":
        case.on_poll = move
    else:
        case.on_lock = move
    return moved


def assert_reused(case, outcome):
    assert outcome == "merged", (case.store.log_path(1).read_text(), case.tracker.comments)
    assert len(case.publications) == 1, "Neutral main movement must keep the original PR head"
    assert git_command(case.history.origin, "rev-parse", f"{case.original_head}^{{tree}}") == case.reviewed_tree, (
        "Keep the verified and reviewed content")
    assert len(case.checked.read_text().splitlines()) == 1, "Reuse the completed machine checks"
    assert case.sleeps == [], "No fresh CI wait may start for a neutral move"
    assert {head for head, _, _ in case.polls} == {case.original_head}
    assert case.landed and case.landed[0][1] == case.original_head
    assert case.tracker.get_issue(1).state == "closed"
    assert case.locks >= 1


@pytest.mark.parametrize("when", ["before-push", "after-ci", "final-lock"])
@pytest.mark.parametrize("files", [
    pytest.param({"docs/neutral.md": "Notes\n"}, id="docs"),
    pytest.param({".claude/agent-memory/fa-orchestrator/MEMORY.new.md": "Role lesson\n"}, id="role-memory"),
    pytest.param({PENDING: locked_test("pending", passing=False) + "\n# Clarified\n"}, id="other-locked"),
])
def test_neutral_move_keeps_head_and_passed_ci(neutral_case, files, when):
    case = neutral_case
    moved = schedule_move(case, files, when)
    outcome = runner.run_issue(1, case.ctx)
    assert moved, ("Exercise a genuinely divergent main", case.tracker.comments)
    assert_reused(case, outcome)
    assert case.landed[0][0] == moved[0], "Merge onto the rechecked current main"
    for path, content in files.items():
        assert git_command(case.history.origin, "show", f"main:{path}") == content.strip()
    if when != "before-push":
        assert case.reports[case.original_head][0]["report"]["base_sha"] == case.base_sha
    if when == "final-lock":
        assert case.locks >= 2, "A move at lock acquisition requires another final check"


def test_source_move_after_neutral_rechecks_and_waits_for_new_head(neutral_case):
    case = neutral_case
    neutral = schedule_move(case, {"docs/neutral.md": "Notes\n"}, "after-ci")
    source = []

    def source_at_lock():
        if not source:
            assert len(case.publications) == 1, "The preceding neutral move must preserve the head"
            case.history.update({"src/widget.py": "VALUE = 1\n"})
            source.append(case.history.tip)

    case.on_lock = source_at_lock
    pending = []

    def status(head):
        if head != case.original_head and not pending:
            pending.append(head)
            return "pending"
        return "pass"

    case.status = status
    assert runner.run_issue(1, case.ctx) == "merged", case.store.log_path(1).read_text()
    assert neutral and source
    assert len(case.publications) == 2, "Only the source move starts a second CI round"
    assert len(case.checked.read_text().splitlines()) == 2, "Recheck the source move once"
    assert pending == [case.publications[-1]] and case.sleeps == [30]
    assert case.landed[0][0] == source[0] and case.landed[0][1] == case.publications[-1]
    assert git_command(case.history.origin, "show", "main:src/widget.py") == "VALUE = 1"


@pytest.mark.parametrize("change", ["approval", "ci", "head"])
def test_neutral_reuse_still_obeys_final_check(neutral_case, change):
    case = neutral_case
    moved = schedule_move(case, {"docs/neutral.md": "Notes\n"}, "after-ci")

    def invalidate():
        if change == "approval":
            case.tracker.remove_labels(1, ["approved"])
        elif change == "ci":
            case.status = lambda head: "fail"
        else:
            pr = case.tracker.find_pr("foragent/issue-1")
            case.tracker.set_pr_head_sha(pr, "f" * 40)

    case.on_lock = invalidate
    outcome = runner.run_issue(1, case.ctx)
    assert moved
    assert len(case.publications) == 1, "Neutral movement must preserve the head before the final refusal"
    assert outcome != "merged"
    assert case.landed == case.tracker.merged_prs == []
    assert case.tracker.get_issue(1).state == "open"
    assert case.history.tip == moved[0], "Refusing the final check must leave main alone"
