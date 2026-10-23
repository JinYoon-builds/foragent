"""Acceptance tests for resume-after-question (all retries). Locked by Foragent; do not edit."""

from dataclasses import asdict
from datetime import timedelta
import json
from pathlib import Path
from shlex import quote
import sys

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
RETRY_HINT = "Keep the selected locale and finish the interrupted implementation."
LIMIT_REASON = "HTTP 429: usage limit reached; wait for the quota reset."


def is_resume(argv):
    return argv[:3] == ["codex", "exec", "resume"] or "--resume" in argv


class QuestionCommands:
    """Keep real executor adapters; fake command I/O as in claude-sessions #153."""

    def __init__(self, case, monkeypatch, *, resume_failure=None, first_exit_code=0):
        from foragent import executors

        self.case, self.calls, self.resume_failure = case, [], resume_failure
        self.first_exit_code = first_exit_code
        monkeypatch.setattr(executors, "run_logged", self.run)
        case.ctx.executor_factory = lambda name: (
            executors.executor_for(name) if name == case.config.executor else case.reviewer
        )
        case.ctx.poll_hook = lambda execute, poll: execute()

    def run(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        assert Path(argv[0]).name == self.case.config.executor, argv
        assert "--bg" not in argv
        code = self.first_exit_code if len(self.calls) == 1 else 0
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
                  resume_failure=None, first_outcome="spec-question",
                  done_when="test -f feature.txt", test_command="true"):
    from foragent.issue import body_hash

    case = make_case(git_history, tmp_path, monkeypatch, executor=executor,
                     reviewer="claude" if executor == "codex" else "codex",
                     language="en", skill_libraries=[], test_command=test_command)
    if executor == "codex":
        case.reviewer.steps = [json.dumps({"result": "(none)"})]
    approval = case.store.load_approval(1)
    body = approval.body.replace("Other features.", UNCHANGED)
    body = body.replace("test -f feature.txt", done_when)
    case.tracker.edit_body(1, body)
    case.tracker.record_label(1, "approved", "human")
    case.store.save_approval(**{**asdict(approval), "body": body, "body_sha": body_hash(body)})
    identity = THREAD_ID if executor == "codex" else SESSION_ID

    def events(include_id, value=identity, *, limit=False):
        if executor == "codex":
            values = ([{"type": "thread.started", "thread_id": value}] if include_id else [])
            values.append({"type": "error", "message": LIMIT_REASON}
                          if limit else {"type": "turn.completed"})
        else:
            values = [{"type": "system", "subtype": "init",
                       **({"session_id": value} if include_id else {})},
                      {"type": "result", "is_error": True, "api_error_status": 429,
                       "result": LIMIT_REASON} if limit else
                      {"type": "result", "result": "Implementation paused or completed"}]
        return "\n".join(json.dumps(value) for value in values) + "\n"

    def pause(kwargs):
        if first_outcome == "timeout":
            case.clock[0] += timedelta(minutes=case.config.max_runtime_minutes + 1)
        elif first_outcome == "spec-question":
            path = kwargs["worktree"] / ".foragent/QUESTION.md"
            path.parent.mkdir(exist_ok=True)
            path.write_text("Which locale?\nA: English\nB: Korean (recommended)\n", encoding="utf-8")
        elif first_outcome == "complete":
            case.implement(kwargs)
        else:
            assert first_outcome == "limit-wait", first_outcome
        return events(session, limit=first_outcome == "limit-wait")

    def complete(kwargs):
        case.implement(kwargs)
        return events(True, identity if is_resume(kwargs["argv"]) else identity + "-fresh")

    case.implementer.steps = [pause, complete]
    commands = QuestionCommands(case, monkeypatch, resume_failure=resume_failure,
                                first_exit_code=1 if first_outcome == "limit-wait" else 0)
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


def retry_and_run(case, *, hint=ANSWER):
    from foragent import orch
    from foragent.runner import run_issue
    from foragent.state import StateStore

    # Reload at the retry boundary: in-memory executor state cannot supply the ID.
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    case.ctx.store = case.store
    orch.retry(case.ctx, 1, hint=hint)
    case.store.start_run(1, branch="foragent/issue-1", worktree=case.store.worktree_path(1))
    assert run_issue(1, case.ctx) == "merged", case.store.log_path(1).read_text()
    assert case.tracker.get_issue(1).state == "closed"
    assert case.store.get_run(1).outcome == "merged"


def assert_resumed_attempt(commands, index, identity, *updates):
    """Observe the real adapter argv and prompt, not an in-memory resume flag."""
    first, call = commands.calls[0], commands.calls[index]
    assert not is_resume(first["argv"])
    assert UNCHANGED in first["prompt"] and "Implement the feature." in first["prompt"]
    argv = call["argv"]
    assert is_resume(argv), f"Expected the recorded worker session to resume; got {argv}"
    if commands.case.config.executor == "codex":
        assert argv[:4] == ["codex", "exec", "resume", identity]
    else:
        assert argv[argv.index("--resume") + 1] == identity
    prompt = call["prompt"]
    for update in updates:
        assert update in prompt, f"Retry prompt lost the changed context or failure reason: {update!r}"
    assert UNCHANGED not in prompt, "A resumed attempt must not repeat the unchanged issue body"
    assert "Implement the feature." not in prompt, "A resumed attempt must not repeat the original goal"
    return prompt


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
    assert_resumed_attempt(commands, 1, identity, NEW_GOAL, NEW_CHECK, ANSWER)
    assert paused.session_id == identity  # #153: persist IDs even on a question exit.
    assert paused.session_executor == executor
    assert paused.session_ids == [identity]
    assert gh == []


@pytest.mark.parametrize("executor", ["codex", "claude"])
@pytest.mark.parametrize("review_result", ["fail", "unusable"])
def test_resume_after_question_each_review_retry_resumes_with_verbatim_feedback(
    git_history, tmp_path, monkeypatch, executor, review_result,
):
    from foragent.runner import run_issue
    from foragent.state import StateStore

    gh = record_gh(monkeypatch)
    case, commands, identity = question_case(
        git_history, tmp_path, monkeypatch, executor, first_outcome="complete",
    )
    feedback = [
        "The locale still falls back to English.\nKeep the user's Korean selection.",
        "The locale is lost when reopening an export.\nPreserve it across reloads.",
    ]
    if review_result == "fail":
        feedback = [f"feature.txt:{index} — " + text.replace("\n", " ")
                    for index, text in enumerate(feedback, 1)]
    case.reviewer.steps = [
        json.dumps({"result": text}) if executor == "codex" else
        ("VERDICT: FAIL\n" + text if review_result == "fail" else text)
        for text in feedback
    ] + case.reviewer.steps
    case.implementer.steps.append(case.implementer.steps[-1])

    assert run_issue(1, case.ctx) == "merged", case.store.log_path(1).read_text()
    assert len(commands.calls) == len(case.reviewer.calls) == 3
    for index, text in enumerate(feedback, 1):
        prompt = assert_resumed_attempt(commands, index, identity, text)
        if review_result == "unusable":
            assert "A reviewer said (verbatim):\n" + text in prompt
    run = StateStore(case.store.root).get_run(1)
    assert run.attempts == 3 and run.session_id == identity
    assert run.session_executor == executor
    assert case.tracker.get_issue(1).state == "closed"
    assert gh == []


@pytest.mark.parametrize("executor", ["codex", "claude"])
@pytest.mark.parametrize("check", ["done-when", "related-test"])
def test_resume_after_question_each_check_retry_resumes_with_failing_output(
    git_history, tmp_path, monkeypatch, executor, check,
):
    from foragent.runner import run_issue
    from foragent.state import StateStore

    gh = record_gh(monkeypatch)
    done_when, test_command = "test -f feature.txt", "true"
    failure = "Done-when output: feature.txt is empty"
    if check == "done-when":
        done_when = f"test -s feature.txt || {{ printf '%s\\n' '{failure}'; exit 1; }}"
        expected_output = f"Command: {done_when}\n{failure}\n"
    else:
        failure = "Related-test output: expected Korean locale"
        git_history.update({
            "feature.py": "def selected_locale():\n    return 'unset'\n",
            "tests/test_feature_locale.py": (
                "from feature import selected_locale\n\n"
                "def test_selected_locale():\n"
                f"    assert selected_locale() == 'ko', {failure!r}\n"
            ),
        })
        test_command = f"{quote(sys.executable)} -B -m pytest -q -p no:cacheprovider"
        expected_output = "AssertionError: " + failure
    case, commands, identity = question_case(
        git_history, tmp_path, monkeypatch, executor, first_outcome="complete",
        done_when=done_when, test_command=test_command,
    )
    initial, complete = case.implementer.steps

    def incomplete(kwargs):
        output = initial(kwargs)
        if check == "done-when":
            (kwargs["worktree"] / "feature.txt").write_text("", encoding="utf-8")
        else:
            (kwargs["worktree"] / "feature.py").write_text(
                "def selected_locale():\n    return 'en'\n", encoding="utf-8",
            )
        return output

    def repaired(kwargs):
        if check == "related-test":
            (kwargs["worktree"] / "feature.py").write_text(
                "def selected_locale():\n    return 'ko'\n", encoding="utf-8",
            )
        return complete(kwargs)

    case.implementer.steps = [incomplete, incomplete, repaired]
    assert run_issue(1, case.ctx) == "merged", case.store.log_path(1).read_text()
    assert len(commands.calls) == 3 and len(case.reviewer.calls) == 1
    for index in (1, 2):
        assert_resumed_attempt(commands, index, identity, expected_output)
    run = StateStore(case.store.root).get_run(1)
    assert run.attempts == 3 and run.session_id == identity
    assert run.session_executor == executor
    assert case.tracker.get_issue(1).state == "closed"
    assert gh == []


@pytest.mark.parametrize("executor", ["codex", "claude"])
def test_resume_after_question_without_recorded_id_falls_back_to_full_prompt(
    git_history, tmp_path, monkeypatch, executor,
):
    from foragent.runner import run_issue
    from foragent.state import StateStore

    gh = record_gh(monkeypatch)
    case, commands, identity = question_case(git_history, tmp_path, monkeypatch, executor, session=False)
    assert run_issue(1, case.ctx) == "spec-question"
    paused = StateStore(case.store.root).get_run(1)
    answer_and_reapprove(case, monkeypatch)
    retry_and_run(case)
    assert len(commands.calls) == 2 and not any(is_resume(call["argv"]) for call in commands.calls)
    assert all(text in commands.calls[1]["prompt"] for text in (UNCHANGED, NEW_GOAL, NEW_CHECK, ANSWER))
    assert hasattr(paused, "session_id"), "#153 must represent an unrecorded session ID as None"
    assert paused.session_id is None and paused.session_ids == []
    assert case.store.get_run(1).session_id == identity + "-fresh"
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
    fresh = commands.calls[2]
    assert_resumed_attempt(commands, 1, identity, NEW_GOAL, NEW_CHECK, ANSWER)
    assert not is_resume(fresh["argv"])
    assert all(text in fresh["prompt"] for text in (UNCHANGED, NEW_GOAL, NEW_CHECK, ANSWER))
    assert case.store.get_run(1).session_id == identity + "-fresh"
    assert gh == []


@pytest.mark.parametrize("executor", ["codex", "claude"])
@pytest.mark.parametrize("stop_reason", ["timeout", "limit-wait"])
def test_resume_after_question_stopped_run_retry_resumes_with_stop_reason_and_hint(
    git_history, tmp_path, monkeypatch, executor, stop_reason,
):
    from foragent.runner import run_issue
    from foragent.state import StateStore

    gh = record_gh(monkeypatch)
    case, commands, identity = question_case(
        git_history, tmp_path, monkeypatch, executor, first_outcome=stop_reason,
    )
    assert run_issue(1, case.ctx) == stop_reason
    paused = StateStore(case.store.root).get_run(1)
    if stop_reason == "limit-wait":
        assert case.store.lane_paused(paused.lane, case.clock[0])
        case.clock[0] += timedelta(minutes=case.config.limit_wait_minutes)
    retry_and_run(case, hint=RETRY_HINT)
    assert len(commands.calls) == 2
    assert_resumed_attempt(commands, 1, identity, stop_reason, RETRY_HINT)
    assert paused.session_id == identity, "#153 must record the stopped worker's session ID"
    assert paused.session_executor == executor and paused.session_ids == [identity]
    assert gh == []
