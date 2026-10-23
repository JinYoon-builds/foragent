"""Acceptance test for resume-after-question. Locked by Foragent; do not edit."""

from dataclasses import asdict
from datetime import timedelta
import json
from pathlib import Path

import pytest

from test_claude_sessions import SESSION_ID, THREAD_ID, sessions_offline
from test_local_truth import record_gh
from test_orch_review_handoff import bind_cli, make_case
from test_skills_autosync import cli_exit, isolated_home


UNCHANGED = (
    "The existing offline export layout, column ordering, archived examples and "
    "historical import formats remain outside this issue and must retain their "
    "documented behavior for every supported locale."
)
NEW_GOAL = "Implement the feature with Korean as the selected locale."
NEW_CHECK = "test -s feature.txt"
ANSWER = "Answer: choose option B; retain the existing export layout."


def is_resume(argv):
    return argv[:3] == ["codex", "exec", "resume"] or "--resume" in argv


class QuestionCommands:
    """Keep real executor adapters; fake command I/O as in claude-sessions #153."""

    def __init__(self, case, monkeypatch, *, resume_failure=None):
        from foragent import executors

        self.case, self.calls, self.resume_failure = case, [], resume_failure
        monkeypatch.setattr(executors, "run_logged", self.run)
        case.ctx.executor_factory = lambda name: (
            executors.executor_for(name) if name == case.config.executor else case.reviewer
        )
        case.ctx.poll_hook = lambda execute, poll: execute()

    def run(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        assert Path(argv[0]).name == self.case.config.executor, argv
        assert "--bg" not in argv
        code = 0
        if is_resume(argv) and self.resume_failure:
            failure, self.resume_failure = self.resume_failure, None
            if failure == "raises":
                raise OSError("Saved implementation session is unavailable")
            code = 1
            event = ({"type": "error", "message": "Saved thread is unavailable"}
                     if argv[0] == "codex" else
                     {"type": "result", "is_error": True, "result": "Saved session is unavailable"})
            output = json.dumps(event) + "\n"
        else:
            # Reuse make_case's Executor.steps/calls rather than fake runner state.
            result = self.case.implementer.run(
                worktree=Path(kwargs["cwd"]), prompt=kwargs["prompt"], model=None,
                timeout_s=kwargs["timeout_s"], log_path=kwargs["log_path"],
                argv=list(argv),
            )
            output = result.last_message
        for name in ("stdout_path", "log_path"):
            path = Path(kwargs[name])
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a" if name == "log_path" else "w", encoding="utf-8") as stream:
                stream.write(output)
        if "-o" in argv:
            Path(argv[argv.index("-o") + 1]).write_text("Implemented" if code == 0 else "", encoding="utf-8")
        return code, False, output


def question_case(git_history, tmp_path, monkeypatch, executor, *, session=True,
                  resume_failure=None, first_outcome="spec-question"):
    from foragent.issue import body_hash

    case = make_case(git_history, tmp_path, monkeypatch, executor=executor,
                     reviewer="claude" if executor == "codex" else "codex",
                     language="en", skill_libraries=[], test_command="true")
    if executor == "codex":
        case.reviewer.steps = [json.dumps({"result": "(none)"})]
    approval = case.store.load_approval(1)
    body = approval.body.replace("Other features.", UNCHANGED)
    case.tracker.edit_body(1, body)
    case.tracker.record_label(1, "approved", "human")
    case.store.save_approval(**{**asdict(approval), "body": body, "body_sha": body_hash(body)})
    identity = THREAD_ID if executor == "codex" else SESSION_ID

    def events(include_id, value=identity):
        if executor == "codex":
            values = ([{"type": "thread.started", "thread_id": value}] if include_id else [])
            values.append({"type": "turn.completed"})
        else:
            values = [{"type": "system", "subtype": "init",
                       **({"session_id": value} if include_id else {})},
                      {"type": "result", "result": "Implementation paused or completed"}]
        return "\n".join(json.dumps(value) for value in values) + "\n"

    def pause(kwargs):
        if first_outcome == "timeout":
            case.clock[0] += timedelta(minutes=case.config.max_runtime_minutes + 1)
        else:
            path = kwargs["worktree"] / ".foragent/QUESTION.md"
            path.parent.mkdir(exist_ok=True)
            path.write_text("Which locale?\nA: English\nB: Korean (recommended)\n", encoding="utf-8")
        return events(session)

    def complete(kwargs):
        case.implement(kwargs)
        return events(True, identity if is_resume(kwargs["argv"]) else identity + "-fresh")

    case.implementer.steps = [pause, complete]
    commands = QuestionCommands(case, monkeypatch, resume_failure=resume_failure)
    return case, commands, identity


def answer_and_reapprove(case, monkeypatch):
    """Use the CLI gate's hash-mismatch/re-approval flow, without launching a worker."""
    from foragent import cli

    main = bind_cli(case, monkeypatch)
    monkeypatch.setattr(cli, "spawn_notify", lambda *args: None)
    monkeypatch.setattr(cli, "spawn_runner", lambda *args: lambda number: pytest.fail("--no-start launched a worker"))
    case.clock[0] += timedelta(minutes=1)
    body = case.tracker.get_issue(1).body.replace("Implement the feature.", NEW_GOAL)
    body = body.replace("test -f feature.txt", NEW_CHECK)
    case.tracker.edit_body(1, body)
    # The orchestrator has answered the question; the gate now checks the edited spec.
    case.tracker.remove_labels(1, ["blocked"])
    argv = ["gate", "tick", "--no-start", "--repo-root", str(case.root)]
    assert cli_exit(main, argv) == 0
    assert case.store.load_approval(1) is None
    assert case.store.load_flag(1).reason == "hash-mismatch"
    case.clock[0] += timedelta(minutes=1)
    case.tracker.record_label(1, "approved", "human")
    assert cli_exit(main, argv) == 0
    approval = case.store.load_approval(1)
    assert approval.body == body and approval.approved_at == case.clock[0].isoformat()


def retry_and_run(case):
    from foragent import orch
    from foragent.runner import run_issue
    from foragent.state import StateStore

    # Reload at the retry boundary: in-memory executor state cannot supply the ID.
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    case.ctx.store = case.store
    orch.retry(case.ctx, 1, hint=ANSWER)
    case.store.start_run(1, branch="foragent/issue-1", worktree=case.store.worktree_path(1))
    assert run_issue(1, case.ctx) == "merged", case.store.log_path(1).read_text()
    assert case.tracker.get_issue(1).state == "closed"
    assert case.store.get_run(1).outcome == "merged"


@pytest.mark.parametrize("executor", ["codex", "claude"])
def test_resume_after_question_reuses_session_with_changed_spec_and_answer(
    git_history, tmp_path, monkeypatch, executor,
):
    from foragent.runner import run_issue
    from foragent.state import StateStore

    gh = record_gh(monkeypatch)
    case, commands, identity = question_case(git_history, tmp_path, monkeypatch, executor)
    assert run_issue(1, case.ctx) == "spec-question"
    paused = StateStore(case.store.root).get_run(1)
    answer_and_reapprove(case, monkeypatch)
    retry_and_run(case)
    assert len(commands.calls) == 2
    argv = commands.calls[1]["argv"]
    assert is_resume(argv), f"Expected the question session to resume; got {argv}"
    if executor == "codex":
        assert argv[:4] == ["codex", "exec", "resume", identity]
    else:
        assert argv[argv.index("--resume") + 1] == identity
    prompt = commands.calls[1]["prompt"]
    assert NEW_GOAL in prompt and NEW_CHECK in prompt and ANSWER in prompt
    assert UNCHANGED in commands.calls[0]["prompt"] and UNCHANGED not in prompt
    assert paused.session_id == identity  # #153: persist IDs even on a question exit.
    assert paused.session_ids == [identity]
    assert gh == []


@pytest.mark.parametrize("executor", ["codex", "claude"])
def test_resume_after_question_without_recorded_id_falls_back_to_full_prompt(
    git_history, tmp_path, monkeypatch, executor,
):
    from foragent.runner import run_issue
    from foragent.state import StateStore

    gh = record_gh(monkeypatch)
    case, commands, _ = question_case(git_history, tmp_path, monkeypatch, executor, session=False)
    assert run_issue(1, case.ctx) == "spec-question"
    paused = StateStore(case.store.root).get_run(1)
    answer_and_reapprove(case, monkeypatch)
    retry_and_run(case)
    assert len(commands.calls) == 2 and not any(is_resume(call["argv"]) for call in commands.calls)
    assert all(text in commands.calls[1]["prompt"] for text in (UNCHANGED, NEW_GOAL, NEW_CHECK, ANSWER))
    assert hasattr(paused, "session_id"), "#153 must represent an unrecorded session ID as None"
    assert paused.session_id is None and paused.session_ids == []
    assert gh == []


@pytest.mark.parametrize("executor", ["codex", "claude"])
@pytest.mark.parametrize("failure", ["raises", "error-result"])
def test_resume_after_question_failed_resume_starts_fresh_and_merges(
    git_history, tmp_path, monkeypatch, executor, failure,
):
    from foragent.runner import run_issue

    gh = record_gh(monkeypatch)
    case, commands, identity = question_case(
        git_history, tmp_path, monkeypatch, executor, resume_failure=failure,
    )
    assert run_issue(1, case.ctx) == "spec-question"
    answer_and_reapprove(case, monkeypatch)
    retry_and_run(case)
    assert len(commands.calls) == 3, "Expected initial, failed resume, then fresh implementation calls"
    resumed, fresh = commands.calls[1:]
    assert is_resume(resumed["argv"]) and identity in resumed["argv"]
    assert not is_resume(fresh["argv"])
    assert ANSWER in resumed["prompt"] and UNCHANGED not in resumed["prompt"]
    assert all(text in fresh["prompt"] for text in (UNCHANGED, NEW_GOAL, NEW_CHECK, ANSWER))
    assert gh == []


@pytest.mark.parametrize("executor", ["codex", "claude"])
def test_resume_after_question_non_question_retry_starts_fresh_despite_session_id(
    git_history, tmp_path, monkeypatch, executor,
):
    from foragent.runner import run_issue
    from foragent.state import StateStore

    gh = record_gh(monkeypatch)
    case, commands, identity = question_case(
        git_history, tmp_path, monkeypatch, executor, first_outcome="timeout",
    )
    assert run_issue(1, case.ctx) == "timeout"
    paused = StateStore(case.store.root).get_run(1)
    retry_and_run(case)
    assert len(commands.calls) == 2 and not any(is_resume(call["argv"]) for call in commands.calls)
    assert UNCHANGED in commands.calls[1]["prompt"] and ANSWER in commands.calls[1]["prompt"]
    assert getattr(paused, "session_id", None) == identity, "#153 must record the timeout's session ID"
    assert gh == []
