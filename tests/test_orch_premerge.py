"""Pre-merge isolation, full-suite discovery, and freshness at the merge boundary."""

from dataclasses import replace
import json
import os
import shlex
import sys
from types import SimpleNamespace

import pytest

from conftest import BASE_TIME, git_command
from foragent import orch
from foragent.config import Config
from foragent.git import GitError, GitRepo
from foragent.runner import RunContext
from foragent.state import StateStore
from foragent.tracker import FakeTracker, TrackerError


@pytest.fixture
def merge_case(git_history, tmp_path):
    h = git_history
    h.update({"feature.txt": "PR version", "suite-ok": "yes",
              "pyproject.toml": "[project]\nname = 'sample'\n",
              "package.json": '{"scripts":{"test":"run-suite"}}',
              "pnpm-lock.yaml": "", "go.mod": "module sample\n"})
    head = h.tip
    git_command(h.origin, "update-ref", "refs/heads/foragent/batch-1", head)
    h.update({"base.txt": "base advanced"})
    git = GitRepo(h.clone)
    tracker = FakeTracker(now=lambda: BASE_TIME)
    pr = tracker.create_pr(head="foragent/batch-1", base="main", title="Batch", body="Keep this body")
    tracker.set_pr_head_sha(pr, head)
    tracker.set_pr_checks(pr, "pass")
    store = StateStore(tmp_path / "state", now=lambda: BASE_TIME)
    config = Config(test_command="test -f suite-ok", repo="owner/repo")
    ctx = RunContext("owner/repo", h.clone, tracker, store, config, git, now=lambda: BASE_TIME)
    (h.clone / "local-draft.txt").write_text("keep me")
    before = git_command(h.clone, "status", "--porcelain")
    worktrees = git_command(h.clone, "worktree", "list", "--porcelain")
    yield SimpleNamespace(ctx=ctx, tracker=tracker, pr=pr, head=head, history=h)
    assert git_command(h.clone, "status", "--porcelain") == before
    assert git_command(h.clone, "worktree", "list", "--porcelain") == worktrees
    assert (h.clone / "local-draft.txt").read_text() == "keep me"


def test_detects_and_runs_every_full_suite_from_pr_head(merge_case, tmp_path, monkeypatch):
    c = merge_case
    c.ctx.config = replace(c.ctx.config, test_command=None)
    programs = tmp_path / "programs"
    programs.mkdir()
    record = tmp_path / "commands.txt"
    for name in ("uv", "pnpm", "go"):
        program = programs / name
        program.write_text('#!/bin/sh\nprintf "%s:%s\\n" "${0##*/}" "$*" >> "$PREMERGE_RECORD"\ntest -f suite-ok\n')
        program.chmod(0o755)
    monkeypatch.setenv("PATH", str(programs) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PREMERGE_RECORD", str(record))
    assert orch.premerge(c.ctx, c.pr)["ready"] is True
    uv, *others = record.read_text().splitlines()
    # pytest also writes JUnit so failures can be compared with the base.
    assert uv.startswith("uv:run pytest --rootdir=. -o junit_family=legacy --junit-prefix= --junitxml=")
    assert others == ["pnpm:test", "go:test ./..."]
    assert not c.tracker.comments
    assert not (c.ctx.store.root / "orch-events.jsonl").exists()


def test_test_artifacts_do_not_affect_trial_merge(merge_case):
    c = merge_case
    c.ctx.config = replace(c.ctx.config, test_command="printf dirty > feature.txt; printf generated > base.txt")
    report = orch.premerge(c.ctx, c.pr)
    assert report["conflicts"] == [] and report["ready"] is True
    assert c.tracker.pr_head_sha(c.pr) == c.head


@pytest.mark.parametrize("action", ["premerge", "merge"])
@pytest.mark.parametrize("content,blocked", [
    ("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> target\n", True),
    (">>>>>>> A valid nested blockquote\n||||||| text\nHeading\n=======\n", False),
])
def test_publication_checks_actual_pr_contents_before_direct_merge(merge_case, action, content, blocked):
    c = merge_case
    base = c.history.tip
    c.history.update({"notes.md": content})
    c.head = c.history.tip
    git_command(c.history.origin, "update-ref", "refs/heads/foragent/batch-1", c.head)
    git_command(c.history.origin, "update-ref", "refs/heads/main", base)
    c.history.tip = base
    c.tracker.set_pr_head_sha(c.pr, c.head)
    # Neither the caller's index nor test-generated files represent the PR.
    c.ctx.config = replace(c.ctx.config, test_command="test -f suite-ok; printf clean > notes.md")
    if action == "premerge":
        report = orch.premerge(c.ctx, c.pr)
        assert report["tests"] == "pass" and report["ci"] == "pass"
        assert report["ready"] is not blocked
        if blocked:
            assert any("notes.md:1" in item for item in report["conflicts"])
        assert not c.tracker.comments and not c.tracker.merged_prs
    elif blocked:
        with pytest.raises(orch.OrchError, match="notes.md:1"):
            orch.merge(c.ctx, c.pr, approved_by="jin")
        assert not c.tracker.comments and not c.tracker.merged_prs
    else:
        orch.merge(c.ctx, c.pr, approved_by="jin")
        assert c.tracker.merged_prs == [c.pr]


@pytest.mark.parametrize("change", ["head", "base", "retarget", "closed", "ci-fail", "ci-pending"])
def test_changes_during_premerge_refuse_merge_before_attribution(merge_case, monkeypatch, change):
    c = merge_case
    calls = []

    def checks(pr):
        calls.append(pr)
        if len(calls) == 1:
            if change == "head":
                c.tracker.set_pr_head_sha(pr, "f" * 40)
            elif change == "base":
                c.history.update({"another.txt": "new base"})
            elif change == "retarget":
                git_command(c.history.origin, "update-ref", "refs/heads/other", c.history.tip)
                c.tracker.update_pr(pr, base="other", title="Batch", body="Keep this body")
            elif change == "closed":
                c.tracker.close_pr(pr)
            return "pass"
        return "pending" if change == "ci-pending" else "fail"

    monkeypatch.setattr(c.tracker, "pr_checks", checks)
    with pytest.raises(orch.OrchError):
        orch.merge(c.ctx, c.pr, approved_by="jin")
    assert not c.tracker.merged_prs
    assert not c.tracker.comments
    assert not (c.ctx.store.root / "orch-events.jsonl").exists()


def test_match_head_guard_catches_change_after_approval_comment(merge_case, monkeypatch):
    c = merge_case
    comment = c.tracker.comment

    def changed(pr, body):
        result = comment(pr, body)
        c.tracker.set_pr_head_sha(pr, "f" * 40)
        return result

    monkeypatch.setattr(c.tracker, "comment", changed)
    with pytest.raises(TrackerError, match="head changed"):
        orch.merge(c.ctx, c.pr, approved_by="jin")
    assert not c.tracker.merged_prs
    assert not (c.ctx.store.root / "orch-events.jsonl").exists()


@pytest.mark.parametrize("failure", ["timeout", "exception", "interrupted", "merge-error", "no-command"])
def test_failure_cleanup_never_leaves_temporary_worktrees(merge_case, monkeypatch, failure):
    c = merge_case
    if failure == "timeout":
        monkeypatch.setattr(orch, "run_logged", lambda *a, **kw: (None, True, ""))
        assert orch.premerge(c.ctx, c.pr)["tests"] == "fail"
        return
    if failure == "no-command":
        c.ctx.config = replace(c.ctx.config, test_command=None)
        monkeypatch.setattr(orch, "detect_test_commands", lambda root: [])
        error = orch.OrchError
    elif failure == "merge-error":
        call = c.ctx.git._call

        def failed(args):
            if "merge" in args:
                raise GitError("merge failed without conflicts")
            return call(args)

        monkeypatch.setattr(c.ctx.git, "_call", failed)
        error = GitError
    else:
        error = OSError if failure == "exception" else SystemExit

        def failed(*a, **kw):
            raise error("test command interrupted")

        monkeypatch.setattr(orch, "run_logged", failed)
    with pytest.raises(error):
        orch.premerge(c.ctx, c.pr)
    assert not c.tracker.merged_prs
    assert not c.tracker.comments


@pytest.mark.parametrize("action", ["premerge", "merge"])
@pytest.mark.parametrize("failed_attempt", [1, 2])
def test_worktree_creation_failure_preserves_original_error(merge_case, monkeypatch, tmp_path,
                                                           action, failed_attempt):
    c = merge_case
    # A pytest failure on the merged head makes premerge check out the base too.
    programs = tmp_path / "programs"
    programs.mkdir()
    (programs / "pytest").write_text(
        "#!/bin/sh\nfor arg; do case $arg in --junitxml=*) out=${arg#--junitxml=};; esac; done\n"
        "printf '<testsuite><testcase file=\"tests/test_a.py\" classname=\"tests.test_a\" name=\"test_x\">"
        "<failure message=\"x\"/></testcase></testsuite>' > \"$out\"\nexit 1\n"
    )
    (programs / "pytest").chmod(0o755)
    monkeypatch.setenv("PATH", str(programs) + os.pathsep + os.environ["PATH"])
    c.ctx.config = replace(c.ctx.config, test_command="pytest")
    add_worktree = c.ctx.git.add_detached_worktree
    creation_error = GitError("PR head object is unavailable")
    attempts = 0

    def add(path, ref):
        nonlocal attempts
        attempts += 1
        if attempts == failed_attempt:
            # A failed creation can leave a directory without a registered worktree.
            path.mkdir()
            raise creation_error
        add_worktree(path, ref)

    monkeypatch.setattr(c.ctx.git, "add_detached_worktree", add)
    with pytest.raises(GitError) as raised:
        if action == "merge":
            orch.merge(c.ctx, c.pr, approved_by="jin")
        else:
            orch.premerge(c.ctx, c.pr)
    assert raised.value is creation_error
    assert attempts == failed_attempt
    assert not c.tracker.merged_prs
    assert not c.tracker.comments
    assert not (c.ctx.store.root / "orch-events.jsonl").exists()


def test_merge_without_ci_uses_clock_and_records_pr_in_event(merge_case):
    c = merge_case
    c.tracker.set_pr_checks(c.pr, "none")
    orch.merge(c.ctx, c.pr, approved_by="jin")
    assert c.tracker.merged_prs == [c.pr]
    assert [comment.body for comment in c.tracker.list_comments(c.pr)] == [
        f"Approved by jin in the orchestrator ({BASE_TIME.isoformat()}). "
        "Pre-merge: tests pass, no conflicts, CI none."
    ]
    event = json.loads((c.ctx.store.root / "orch-events.jsonl").read_text())
    assert event["kind"] == "merge" and event["issue"] is None
    assert f"PR #{c.pr}" in event["summary"] and c.head in event["summary"]


@pytest.mark.parametrize("approval,at", [("  ", None), ("jin", "invalid-time")])
def test_invalid_attribution_is_rejected_before_checks(merge_case, approval, at):
    c = merge_case
    with pytest.raises(orch.OrchError):
        orch.merge(c.ctx, c.pr, approved_by=approval, at=at)
    assert not c.tracker.comments and not c.tracker.merged_prs


@pytest.mark.parametrize("body,closed", [("Closes #10", True), ("Related to #10", False)])
def test_orch_merge_records_linked_issue_state_returned_by_github(merge_case, monkeypatch, body, closed):
    c = merge_case
    c.tracker.add_issue(10, "Feature", labels=["blocked", "needs-human"])
    c.ctx.tracker.get_issue(10)
    c.tracker.created_prs = [replace(pr, head="foragent/issue-10", body=body)
                             for pr in c.tracker.created_prs]
    merge = c.tracker.merge_pr

    def github_merge(pr, **kwargs):
        merge(pr, **kwargs)
        if closed:
            c.tracker.close_issue(10)

    monkeypatch.setattr(c.tracker, "merge_pr", github_merge)
    orch.merge(c.ctx, c.pr, approved_by="jin")
    row, = c.ctx.store.local_issues()
    assert row["number"] == 10 and row["pr_number"] == c.pr and row["merged"] is True
    assert row["state"] == ("closed" if closed else "open")
    watched, = orch.watch(c.ctx)
    assert watched["blocked"] == ([] if closed else [10])


@pytest.mark.parametrize("failure", [403, 429])
def test_orch_merge_completes_when_linked_issue_refresh_fails(merge_case, monkeypatch, failure):
    c = merge_case
    c.tracker.add_issue(10, "Feature", labels=["blocked", "needs-human"])
    c.ctx.tracker.get_issue(10)
    c.tracker.created_prs = [replace(pr, head="foragent/issue-10", body="Closes #10")
                             for pr in c.tracker.created_prs]
    finished = []
    get_issue = c.tracker.get_issue

    def unavailable(number):
        # The pre-merge read supplies the PR title; only the post-merge refresh fails.
        if number != 10 or not c.tracker.merged_prs:
            return get_issue(number)
        assert number == 10 and c.tracker.merged_prs == [c.pr]
        raise TrackerError(f"HTTP {failure}")

    def after_merge(ctx, pr, base):
        event = json.loads((ctx.store.root / "orch-events.jsonl").read_text())
        assert event["kind"] == "merge" and c.head in event["summary"]
        finished.append((pr, base))

    monkeypatch.setattr(c.tracker, "get_issue", unavailable)
    monkeypatch.setattr(orch, "after_merge", after_merge)
    summary = orch.merge(c.ctx, c.pr, approved_by="jin")
    assert "merged" in summary
    assert c.tracker.merged_prs == [c.pr]
    assert finished == [(c.pr, "main")]
    row, = c.ctx.store.local_issues()
    assert row["number"] == 10 and row["pr_number"] == c.pr and row["merged"] is True


def test_premerge_ci_accepts_and_caches_an_entirely_excluded_acceptance_suite(merge_case, monkeypatch):
    from conftest import ACCEPTANCE

    c = merge_case
    c.ctx.config = replace(c.ctx.config, runner_unit_tests="ci",
                           test_command=f"{shlex.quote(sys.executable)} -m pytest")
    c.ctx.store.save_approval(
        1, body="pending", body_sha="body", acceptance_blobs={ACCEPTANCE: "blob"},
        approved_by="human", approved_at=BASE_TIME.isoformat(),
    )
    run_logged = orch.run_logged

    def process(argv, *, cwd, **kwargs):
        assert git_command(cwd, "rev-parse", "HEAD") != c.history.tip, "Empty base must not spawn pytest"
        return run_logged(argv, cwd=cwd, **kwargs)

    monkeypatch.setattr(orch, "run_logged", process)
    for _ in range(2):
        report = orch.premerge(c.ctx, c.pr)
        assert report["ready"] is True and report["tests"] == "pass"
        assert report["broken"] == [] and report["failing_on_base"] == []
    log = (c.ctx.store.root / "runs" / f"premerge-pr-{c.pr}.log").read_text()
    assert log.count("cache=hit") == 1


@pytest.mark.parametrize("mode", ["ci", "local"])
@pytest.mark.parametrize("owned", [True, False])
def test_premerge_head_only_pending_lock_exclusion_keeps_unowned_failures(merge_case, mode, owned):
    c = merge_case
    base = c.history.tip
    locked = "tests/acceptance/test_new_lock.py"
    c.history.update({locked: '"""Locked by Foragent; do not edit."""\n'
                      'def test_pending():\n    assert False, "not implemented yet"\n'})
    head = c.history.tip
    git_command(c.history.origin, "update-ref", "refs/heads/foragent/batch-1", head)
    git_command(c.history.origin, "update-ref", "refs/heads/main", base)
    c.history.tip = base
    c.tracker.set_pr_head_sha(c.pr, head)
    c.ctx.config = replace(c.ctx.config, runner_unit_tests=mode,
                           test_command=f"{shlex.quote(sys.executable)} -m pytest -q")
    if owned:
        c.tracker.add_issue(99, "Pending feature", labels=["approved"])
        c.ctx.store.save_approval(
            99, body="pending", body_sha="body", acceptance_blobs={locked: "blob"},
            approved_by="human", approved_at=BASE_TIME.isoformat(),
        )
    assert c.ctx.git.blob_sha(base, locked) is None
    report = orch.premerge(c.ctx, c.pr)
    assert report["broken"] == ([] if owned else [f"{locked}::test_pending"])
    assert report["failing_on_base"] == []
    assert report["tests"] == ("pass" if owned else "fail")
    assert report["ready"] is owned
    if owned:
        log = (c.ctx.store.root / "runs" / f"premerge-pr-{c.pr}.log").read_text()
        assert "Acceptance baseline skipped 1 files owned by open issues: #99" in log


@pytest.mark.parametrize("failure", [False, True])
def test_premerge_suite_releases_slot_and_propagates_it_to_nested_tests(merge_case, monkeypatch, failure):
    from contextlib import contextmanager

    from foragent import capacity

    c = merge_case
    holding = []

    @contextmanager
    def slot(ceiling, *, waiting):
        assert ceiling == c.ctx.config.test_slots
        holding.append(True)
        try:
            yield
        finally:
            holding.pop()

    def process(*args, **kwargs):
        assert holding == [True]
        assert kwargs["env"] == capacity.HELD_ENV
        if failure:
            raise OSError("suite could not start")
        return 0, False, ""

    monkeypatch.setattr(capacity, "hold_test_slot", slot)
    monkeypatch.setattr(orch, "run_logged", process)
    if failure:
        with pytest.raises(OSError, match="suite could not start"):
            orch.premerge(c.ctx, c.pr)
    else:
        assert orch.premerge(c.ctx, c.pr)["waited_seconds"] == 0
    assert holding == []


def queue_events(c):
    path = c.ctx.store.root / "merge-queue.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.parametrize("holder", ["live", "finished", None])
def test_orch_merge_takes_the_fifo_merge_turn(merge_case, holder):
    """A human merge never overtakes a live turn holder and records its own turn (#712)."""
    from foragent import run_merge_turn as turn

    c = merge_case
    store = c.ctx.store
    if holder is not None:
        store.start_run(7, branch="foragent/issue-7", worktree=store.worktree_path(7))
        turn.join(c.ctx, turn.seat(c.ctx, 7, "main"), "a" * 40)
        if holder == "finished":
            store.finish_run(7, outcome="merged")
    before = queue_events(c)
    if holder == "live":
        with pytest.raises(orch.OrchError, match="waits for the merge turn: #7 holds it"):
            orch.merge(c.ctx, c.pr, approved_by="jin")
        assert c.tracker.merged_prs == []
        assert queue_events(c) == before
        return
    orch.merge(c.ctx, c.pr, approved_by="jin")
    assert c.tracker.merged_prs == [c.pr]
    own = queue_events(c)[len(before):]
    base = git_command(c.history.origin, "rev-parse", "main")
    assert [(row["n"], row["event"]) for row in own] == [(c.pr, "ready"), (c.pr, "turn")]
    assert own[1]["members"] == [c.pr] and own[1]["base_sha"] == base and own[1]["result"] == "merged"
    assert own[0]["head_sha"] == c.head
    # Its entry never blocks a later run: no run carries its started_at.
    store.start_run(8, branch="foragent/issue-8", worktree=store.worktree_path(8))
    later = turn.seat(c.ctx, 8, "main")
    turn.join(c.ctx, later, "b" * 40)
    assert turn.holds_turn(store, later)


def test_orch_merge_failure_records_failed_turn_and_withdraws(merge_case, monkeypatch):
    c = merge_case
    monkeypatch.setattr(c.tracker, "merge_pr", lambda *a, **kw: (_ for _ in ()).throw(TrackerError("refused")))
    with pytest.raises(TrackerError, match="refused"):
        orch.merge(c.ctx, c.pr, approved_by="jin")
    events = [(row["event"], row.get("result")) for row in queue_events(c)]
    assert events == [("ready", None), ("turn", "merged"), ("turn", "failed"), ("withdrawn", None)]


def test_orch_merge_claims_the_turn_atomically_against_a_joining_run(merge_case, monkeypatch):
    """A run joining while orch.merge checks the queue lands behind it, never between check and join."""
    import threading
    from foragent import run_merge_turn as turn

    c = merge_case
    store = c.ctx.store
    store.start_run(6, branch="foragent/issue-6", worktree=store.worktree_path(6))
    turn.join(c.ctx, turn.seat(c.ctx, 6, "main"), "a" * 40)
    store.finish_run(6, outcome="merged")
    store.start_run(7, branch="foragent/issue-7", worktree=store.worktree_path(7))
    joiner = threading.Thread(target=turn.join, args=(c.ctx, turn.seat(c.ctx, 7, "main"), "b" * 40))
    append = turn._append

    def join_before_claim_appends(store, row):
        if row["n"] == c.pr and row["event"] == "ready":
            # Between the claim's check and its append, a run tries to join. A non-atomic claim
            # lets it finish here; an atomic one keeps it blocked on the state lock.
            joiner.start()
            joiner.join(1)
        append(store, row)

    monkeypatch.setattr(turn, "_append", join_before_claim_appends)
    orch.merge(c.ctx, c.pr, approved_by="jin")
    joiner.join(5)
    assert not joiner.is_alive()
    ready = [row["n"] for row in queue_events(c) if row["event"] == "ready"]
    assert ready == [6, c.pr, 7], "The run that joined during the claim must queue behind the human merge"


def test_orch_merge_rechecks_the_base_after_waiting_for_the_merge_lock(merge_case, monkeypatch):
    c = merge_case
    lock = c.ctx.store.merge_lock
    from contextlib import contextmanager

    @contextmanager
    def base_moves_while_waiting():
        c.history.update({"other.txt": "Another merge landed while waiting for the lock"})
        with lock():
            yield

    monkeypatch.setattr(c.ctx.store, "merge_lock", base_moves_while_waiting)
    with pytest.raises(orch.OrchError, match="head or base changed"):
        orch.merge(c.ctx, c.pr, approved_by="jin")
    assert c.tracker.merged_prs == []
    assert c.tracker.list_comments(c.pr) == [], "Approval is recorded only for a fresh base"
    assert [row["event"] for row in queue_events(c)] == ["ready", "withdrawn"]


def test_orch_merge_refuses_a_dream_pr_that_predates_a_redo(merge_case):
    """#1231: after `fa orch dream --redo`, a dream PR from an older prompt is never merged."""
    from datetime import timedelta

    from foragent import orch_dream

    c = merge_case
    git_command(c.history.origin, "update-ref", "refs/heads/foragent/dream-alpha", c.head)
    pr = c.tracker.create_pr(head="foragent/dream-alpha", base="main", title="Dream", body="Old")
    c.tracker.set_pr_head_sha(pr, c.head)
    c.tracker.set_pr_checks(pr, "pass")
    c.ctx.config = replace(c.ctx.config, lanes={"alpha": {"paths": ["a/**"]}})
    earlier = BASE_TIME - timedelta(hours=1)
    orch_dream.record_published(c.ctx.store, "alpha", 1, earlier, covered_until=earlier, pr=pr,
                                inputs={}, outputs={})
    assert orch_dream.redo(c.ctx, "alpha") == 0
    with pytest.raises(orch.OrchError, match=f"dream PR #{pr} predates a redo of lane alpha"):
        orch.merge(c.ctx, pr, approved_by="jin")
    assert c.tracker.merged_prs == []
