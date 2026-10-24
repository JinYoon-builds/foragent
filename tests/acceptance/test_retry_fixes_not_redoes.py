"""Acceptance test for retry-fixes. Locked by Foragent; do not edit."""

from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import subprocess

import pytest

from conftest import git_command
from foragent import runner, scheduler
from foragent.executors import ExecResult
from foragent.issue import body_hash
from foragent.state import StateStore
from test_orch_review_handoff import bind_cli, make_case, tick_context
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401


BRANCH = "foragent/issue-1"
DEFAULT_RETRY_HINT = (
    "Keep the branch's work; for each recorded finding, name the defect class it belongs to, "
    "find every other instance of that class in the change, fix them all, "
    "and state the class and the instances found."
)
REVIEW_FEEDBACK_INSTRUCTIONS = (
    "For each review finding, name the defect class it belongs to, "
    "find every other instance of that class in the change, fix or rebut each instance, "
    "and state the class and the instances found."
)
REVIEWER_CLASS_INSTRUCTIONS = (
    "For each finding, say whether it is one instance of a broader defect class "
    "and where else to look in the change."
)
CLASS_FINDINGS = (
    "feature.txt:1 — the status writer drops unknown fields from the saved run record",
    "kept-work.txt:1 — the progress writer drops unknown fields from the saved run record",
)
STOP_POINT = "Stopped after adding the parser skeleton; empty-input repair is still pending."
CHECK_FINDING = "Empty input still crashes the saved parser (offline check 47)."
REVIEW_FINDING = "feature.txt:1 — preserve the selected locale on empty input"
KEPT_WORK = "Parser skeleton already implemented; keep this work.\n"
OWN_FILES = ("feature.txt", "parser.txt", "locale.txt", "kept-work.txt")


@pytest.fixture(autouse=True)
def local_processes_only(offline_acceptance, monkeypatch):
    """Shared fixtures isolate HOME/network/signals; allow only local Git/checks."""
    popen = subprocess.Popen

    def local(argv, *args, **kwargs):
        assert isinstance(argv, (list, tuple)) and argv
        assert Path(argv[0]).name in {"git", "sh", "bash"}, (
            f"A real executor or external service escaped the offline fakes: {argv}"
        )
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", local)


def _case(git_history, tmp_path, monkeypatch, **config):
    case = make_case(git_history, tmp_path, monkeypatch, **{
        "skill_libraries": [], "test_command": "true", "reviewer": "none",
        "max_retries": 0, "conflict_retries": 2, **config,
    })
    case.ctx.poll_hook = lambda execute, poll: execute()
    case.tracker.record_label(1, "approved", "human")

    # Extend the shared scripted executor only at its I/O boundary, so it can
    # also return session IDs and failed resume results. Prompts remain real.
    def execute(**kwargs):
        case.implementer.calls.append(dict(kwargs))
        assert case.implementer.steps, "Unexpected implementation turn"
        step = case.implementer.steps.pop(0)
        result = step(kwargs) if callable(step) else step
        return result if isinstance(result, ExecResult) else ExecResult(
            0, False, result or "", kwargs["log_path"],
        )

    monkeypatch.setattr(case.implementer, "run", execute)
    return case


def _run(case):
    outcome = runner.run_issue(1, case.ctx)
    assert outcome != "error", case.store.log_path(1).read_text(encoding="utf-8")
    return outcome


def _retry(case, monkeypatch, *, hint=None):
    """Public CLI queues the retry; a tick drains writes before the next worker."""
    case.clock[0] += timedelta(minutes=1)
    main = bind_cli(case, monkeypatch)
    argv = ["orch", "retry", "1"] + ([] if hint is None else ["--hint", hint])
    assert main(argv) == 0
    scheduler.tick(tick_context(case, start_runs=False))
    case.store.start_run(1, branch=BRANCH, worktree=case.store.worktree_path(1))


def _write_attempt(kwargs):
    (kwargs["worktree"] / "feature.txt").write_text("parser skeleton\n", encoding="utf-8")
    (kwargs["worktree"] / "kept-work.txt").write_text(KEPT_WORK, encoding="utf-8")
    with kwargs["log_path"].open("a", encoding="utf-8") as log:
        log.write(STOP_POINT + "\n")


def _failed_check_case(git_history, tmp_path, monkeypatch, *, session_id=None):
    case = _case(git_history, tmp_path, monkeypatch)
    approval = case.store.load_approval(1)
    # The diagnostic is read from a file, so its text is not already present
    # in the approved command copied into every fresh implementation prompt.
    body = approval.body.replace("test -f feature.txt", "cat check-output.txt; test -f repaired.txt")
    case.tracker.update_issue(1, title="Implement feature", body=body)
    case.store.save_approval(**replace(approval, body=body, body_sha=body_hash(body)).__dict__)

    def unfinished(kwargs):
        _write_attempt(kwargs)
        (kwargs["worktree"] / "check-output.txt").write_text(CHECK_FINDING + "\n", encoding="utf-8")
        return ExecResult(0, False, STOP_POINT, kwargs["log_path"], session_id=session_id)

    case.implementer.steps = [unfinished]
    assert _run(case) == "retries-exceeded"
    case.previous_head = git_command(case.root, "rev-parse", BRANCH)
    assert CHECK_FINDING in case.store.log_path(1).read_text(encoding="utf-8")
    assert git_command(case.root, "show", f"{BRANCH}:kept-work.txt") == KEPT_WORK.strip()
    return case


def _finish_check(kwargs):
    assert (kwargs["worktree"] / "kept-work.txt").read_text(encoding="utf-8") == KEPT_WORK
    (kwargs["worktree"] / "repaired.txt").write_text("fixed\n", encoding="utf-8")
    return "Fixed the failing check without repeating the completed implementation."


def _assert_previous_attempt(prompt, *, head, finding):
    # Use D74's handoff heading, without requiring a new summary API or an
    # extra model turn: these short facts all exist in local Git/run artifacts.
    assert "Previous attempt" in prompt, (
        "Fresh retry prompt lacks a Previous attempt summary from local records"
    )
    summary = prompt.partition("Previous attempt")[2]
    for fact in (BRANCH, "feature.txt", "kept-work.txt", finding, STOP_POINT):
        assert fact in summary, f"Previous attempt summary lost local fact: {fact!r}"
    assert head[:7] in summary, f"Previous attempt summary omits branch commit {head[:7]}"


def test_tier_escalation_fresh_session_keeps_review_and_branch_context(git_history, tmp_path, monkeypatch):
    case = _case(git_history, tmp_path, monkeypatch, reviewer="codex", max_retries=1,
                 review_scope="pr-body", tiers={
                     "s": {"codex_effort": "medium", "claude_model": "offline-small"},
                     "m": {"codex_effort": "high", "claude_model": "offline-medium"},
                     "l": {"codex_effort": "xhigh", "claude_model": "offline-large"},
                 })
    case.tracker.add_labels(1, ["size:s"])
    previous_heads = []

    def failed_review(kwargs):
        # pr-body review has saved the first attempt's real staged tree.
        previous_heads.append(git_command(kwargs["worktree"], "rev-parse", "HEAD"))
        return "VERDICT: FAIL\n" + REVIEW_FINDING

    def repair(kwargs):
        assert (kwargs["worktree"] / "kept-work.txt").read_text(encoding="utf-8") == KEPT_WORK
        return case.implement(kwargs)

    # No resumable ID is available: both tiers must receive a fresh prompt.
    case.implementer.steps = [_write_attempt, repair]
    case.reviewer.steps = [failed_review, "VERDICT: PASS"]
    assert _run(case) == "merged"
    calls = case.implementer.calls
    assert [call["model"] for call in calls] == ["offline-small", "offline-medium"]
    assert all(call.get("resume_session_id") is None for call in calls)
    review = case.store.root / "runs/issue-1/review-1.txt"
    assert REVIEW_FINDING in review.read_text(encoding="utf-8")
    _assert_previous_attempt(calls[1]["prompt"], head=previous_heads[0], finding=REVIEW_FINDING)


@pytest.mark.parametrize("session_loss", ["missing", "rejected", "launch-error"])
def test_unresumable_retry_summarizes_saved_work_checks_and_stop(
    git_history, tmp_path, monkeypatch, session_loss,
):
    identity = None if session_loss == "missing" else "offline-previous-session"
    case = _failed_check_case(git_history, tmp_path, monkeypatch, session_id=identity)

    def cannot_resume(kwargs):
        assert kwargs.get("resume_session_id") == identity
        if session_loss == "launch-error":
            raise OSError("Offline session storage is unavailable")
        return ExecResult(1, False, "The saved session no longer exists", kwargs["log_path"])

    case.implementer.steps = ([] if identity is None else [cannot_resume]) + [_finish_check]
    _retry(case, monkeypatch)
    assert _run(case) == "merged"
    fresh = case.implementer.calls[-1]
    assert fresh.get("resume_session_id") is None
    assert len(case.implementer.calls) == (2 if identity is None else 3)
    _assert_previous_attempt(fresh["prompt"], head=case.previous_head, finding=CHECK_FINDING)


@pytest.mark.parametrize("resumable", [False, True], ids=["fresh", "resumed"])
@pytest.mark.parametrize("hint", [None, "Keep the Korean locale while repairing empty-input handling."],
                         ids=["default", "with-hint"])
def test_orch_retry_delivers_preservation_default_before_optional_hint(
    git_history, tmp_path, monkeypatch, hint, resumable,
):
    identity = "offline-resumable-session" if resumable else None
    case = _failed_check_case(git_history, tmp_path, monkeypatch, session_id=identity)
    case.implementer.steps = [_finish_check]
    _retry(case, monkeypatch, hint=hint)
    assert _run(case) == "merged"
    call = case.implementer.calls[-1]
    assert call.get("resume_session_id") == identity
    prompt = call["prompt"]
    assert DEFAULT_RETRY_HINT in prompt, (
        "fa orch retry must deliver its keep-work/defect-class sweep default to the implementer"
    )
    assert "fix only the recorded findings" not in prompt.casefold(), (
        "The earlier instance-only retry instruction contradicts the defect-class sweep"
    )
    if hint is not None:
        assert hint in prompt, "The explicit --hint was lost before reaching the implementer"
        assert prompt.index(DEFAULT_RETRY_HINT) < prompt.index(hint), (
            "The explicit --hint must be added after the preservation default"
        )


@pytest.mark.parametrize("resumable", [False, True], ids=["fresh", "resumed"])
@pytest.mark.parametrize("review_format", ["verdict", "unstructured"])
def test_review_feedback_prompt_requests_class_sweep_and_each_instance_fix_or_rebuttal(
    git_history, tmp_path, monkeypatch, resumable, review_format,
):
    """Classification belongs to the implementer, including on automatic review retries."""
    case = _case(git_history, tmp_path, monkeypatch, reviewer="codex", max_retries=1)
    identity = "offline-review-feedback-session" if resumable else None

    def implement(kwargs):
        _write_attempt(kwargs)
        return ExecResult(0, False, STOP_POINT, kwargs["log_path"], session_id=identity)

    def repair(kwargs):
        assert (kwargs["worktree"] / "kept-work.txt").read_text(encoding="utf-8") == KEPT_WORK
        return case.implement(kwargs)

    # Deliberately supply only examples, without a class label or search advice.
    # The harness must give the agent that context instead of deciding the class.
    feedback = "\n".join(CLASS_FINDINGS)
    raw_review = ("VERDICT: FAIL\n" if review_format == "verdict" else "") + feedback
    case.implementer.steps = [implement, repair]
    case.reviewer.steps = [raw_review, "VERDICT: PASS"]
    assert _run(case) == "merged"
    assert len(case.implementer.calls) == 2
    call = case.implementer.calls[1]
    assert call.get("resume_session_id") == identity
    prompt = call["prompt"]
    for finding in CLASS_FINDINGS:
        assert finding in prompt, f"Review-feedback round lost the recorded example: {finding!r}"
    assert REVIEW_FEEDBACK_INSTRUCTIONS in prompt, (
        "Review-feedback prompt must ask the implementer to name each defect class, "
        "find every other instance in the change, fix or rebut each, and report the class and instances"
    )


@pytest.mark.parametrize("reviewer,scope", [
    ("codex", "claude-local"), ("claude", "claude-local"), ("claude", "pr-body"),
], ids=["codex-prompt", "claude-local-context", "claude-pr-context"])
def test_reviewer_receives_class_and_other_locations_request(
    git_history, tmp_path, monkeypatch, reviewer, scope,
):
    """Inspect the reviewer input at the executor boundary for each context delivery path."""
    from foragent.git import REVIEW_SCOPE_FILE

    case = _case(git_history, tmp_path, monkeypatch, reviewer=reviewer, review_scope=scope,
                 executor="claude" if reviewer == "codex" else "codex")
    received = []

    def review(kwargs):
        context = kwargs["prompt"]
        if reviewer == "claude" and scope == "claude-local":
            context += (kwargs["worktree"] / REVIEW_SCOPE_FILE).read_text(encoding="utf-8")
        elif reviewer == "claude":
            context += next(pr.body for pr in case.tracker.list_prs("open", BRANCH)
                            if pr.number == kwargs["review_pr"])
        received.append(context)
        return json.dumps({"result": "(none)"}) if reviewer == "claude" else "VERDICT: PASS"

    case.implementer.steps = [_write_attempt]
    case.reviewer.steps = [review]
    assert _run(case) == "merged"
    assert len(case.reviewer.calls) == len(received) == 1
    assert REVIEWER_CLASS_INSTRUCTIONS in received[0], (
        "Reviewer input must ask whether each finding is one instance of a broader defect class "
        "and where else to look in the change"
    )


def _conflicting_case(git_history, tmp_path, monkeypatch, *, conflicts, entrypoint):
    git_history.update({name: f"original {name}\n" for name in OWN_FILES})
    case = _case(git_history, tmp_path, monkeypatch)
    case.conflicts = OWN_FILES[:conflicts]
    case.resolution_turns, case.recreations = [], []
    add_worktree = case.git.add_worktree

    def observe_creation(path, branch, start_ref):
        case.recreations.append(case.git.ref_sha(start_ref))
        return add_worktree(path, branch, start_ref)

    monkeypatch.setattr(case.git, "add_worktree", observe_creation)

    def move_base():
        # These target-only files must not inflate the PR's four-file denominator.
        case.history.update({**{name: f"base change {name}\n" for name in case.conflicts},
                             **{f"base-only-{i}.txt": "unrelated base work\n" for i in range(8)}})
        case.moved_base = case.history.tip

    def implement(kwargs):
        for name in OWN_FILES:
            (kwargs["worktree"] / name).write_text(f"issue change {name}\n", encoding="utf-8")
        if entrypoint == "saved-retry":
            question = kwargs["worktree"] / ".foragent/QUESTION.md"
            question.parent.mkdir(exist_ok=True)
            question.write_text("Which locale? A: Korean (recommended); B: English.\n", encoding="utf-8")
        else:
            move_base()
        return "Implemented all four issue files."

    def resolve_or_reimplement(kwargs):
        root = kwargs["worktree"]
        phase = case.store.get_run(1).phase
        markers = [name for name in case.conflicts if "<<<<<<<" in (root / name).read_text(encoding="utf-8")]
        case.resolution_turns.append({"phase": phase, "markers": markers,
                                      "head": git_command(root, "rev-parse", "HEAD"),
                                      "prompt": kwargs["prompt"]})
        if phase == "resolving-conflict" and len(case.resolution_turns) <= case.config.conflict_retries:
            return "Could not resolve the recorded conflict markers in place."
        # Let today's unwanted rebuild complete so assertions diagnose the
        # lost branch, rather than failing because the fake ran out of steps.
        for name in OWN_FILES:
            text = (f"base change {name}\n" if name in case.conflicts else "") + f"issue change {name}\n"
            (root / name).write_text(text, encoding="utf-8")
        return "Finished preserving both base and issue features."

    case.implementer.steps = [implement] + [resolve_or_reimplement] * (case.config.conflict_retries + 1)
    if entrypoint == "saved-retry":
        assert _run(case) == "spec-question"
        move_base()
        _retry(case, monkeypatch)
    return case


@pytest.mark.parametrize("entrypoint", ["publish", "saved-retry"])
@pytest.mark.parametrize("conflicts", [1, 2], ids=["minority", "exactly-half"])
def test_exhausted_conflicts_at_or_below_half_never_rebuild_the_pr(
    git_history, tmp_path, monkeypatch, conflicts, entrypoint,
):
    case = _conflicting_case(git_history, tmp_path, monkeypatch,
                             conflicts=conflicts, entrypoint=entrypoint)
    outcome = _run(case)
    assert case.moved_base not in case.recreations, (
        f"Exhausted {conflicts}/{len(OWN_FILES)} conflict rebuilt from base; "
        "keep the PR's work and resolve in place or stop for the orchestrator"
    )
    if outcome != "merged":
        run = StateStore(case.store.root).get_run(1)
        assert "blocked" in case.tracker.get_issue(1).labels, "An unresolved conflict must reach the orchestrator"
        assert set(case.conflicts) <= set(run.merge_conflicts), "Stopping lost the unresolved conflict paths"
    assert case.resolution_turns, "In-place resolution must be tried before stopping"


@pytest.mark.parametrize("entrypoint", ["publish", "saved-retry"])
def test_majority_rebuild_follows_in_place_budget_and_persists_count_and_reason(
    git_history, tmp_path, monkeypatch, entrypoint,
):
    case = _conflicting_case(git_history, tmp_path, monkeypatch, conflicts=3, entrypoint=entrypoint)
    assert _run(case) == "merged"
    turns = case.resolution_turns
    budget = case.config.conflict_retries
    assert [turn["phase"] for turn in turns[:budget]] == ["resolving-conflict"] * budget
    assert all(set(turn["markers"]) == set(case.conflicts) for turn in turns[:budget])
    assert len({turn["head"] for turn in turns[:budget]}) == 1, "Resolution attempts discarded the PR history"
    assert len(turns) == budget + 1 and turns[-1]["head"] == case.moved_base
    assert case.recreations.count(case.moved_base) == 1, "Only one from-scratch rebuild is needed"

    # Read fresh objects AND the durable rows consumed by facts, not an
    # Implementation.rebuilt flag or a human-only log message.
    store = StateStore(case.store.root)
    current = json.loads((store.root / "runs/1.json").read_text(encoding="utf-8"))
    history = [json.loads(line) for line in (store.root / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    finished = next(row for row in reversed(history) if row["number"] == 1)
    for row in (current, finished, vars(store.get_run(1)), vars(store.read_history()[-1])):
        assert row.get("rebuild_count") == 1, (
            "From-scratch rebuild count is missing from the persisted run record (rebuild_count=1)"
        )
        reason = row.get("rebuild_reason")
        assert isinstance(reason, str) and reason.strip(), "Persist rebuild_reason for fa orch facts"
        assert "3/4" in reason and str(budget) in reason, (
            "Rebuild reason must state the PR's own conflict fraction (3/4) and exhausted resolution budget (2)"
        )
