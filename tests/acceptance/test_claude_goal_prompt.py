"""Acceptance test for claude-goal-prompt. Locked by Foragent; do not edit."""

from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import tempfile

import pytest

from test_orch_review_handoff import make_case
from test_skills_autosync import isolated_home


GOAL_ERROR = "Goal condition is limited to 4000 characters (got 30794)"


@pytest.fixture(autouse=True)
def offline_goal_runs(isolated_home, tmp_path, monkeypatch):
    """Keep executor temporaries local and reject commands outside the fixture."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    popen = subprocess.Popen

    def local_process(argv, *args, **kwargs):
        assert isinstance(argv, (list, tuple)) and argv
        assert Path(argv[0]).name in {"git", "sh", "bash"}, (
            f"An external command escaped the fake executor: {argv}"
        )
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", local_process)


def input_messages(argv, stdin):
    """Observe message boundaries, including stream-json user messages.

    A blank line never terminates a slash command's argument. All text after
    /goal in ONE message belongs to the condition, as Claude interprets it.
    Separate argv values also cover positional and system prompts.
    """
    messages = list(argv[1:])
    if stdin is None:
        return messages
    stream_json = "--input-format=stream-json" in argv or (
        "--input-format" in argv and argv[argv.index("--input-format") + 1] == "stream-json"
    )
    if not stream_json:
        return messages + [stdin]
    for line in stdin.splitlines():
        event = json.loads(line)
        message = event.get("message", event)
        content = message.get("content", "")
        if isinstance(content, list):
            content = "\n".join(part["text"] for part in content if part.get("type") == "text")
        messages.append(content)
    return messages


def delivered_files(case, argv, messages, worktree):
    """Snapshot context files at launch, before runner cleanup removes them.

    Count auto-loaded Claude context and files actually referenced by the
    command or its messages. An unreferenced saved approval is not delivery.
    This allows a separate message, system prompt, or referenced prompt file.
    """
    references = "\n".join(messages)
    automatic = {worktree / name for name in ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md")}
    candidates = (automatic | set(worktree.rglob("*")) | set(case.store.root.rglob("*"))
                  | set(Path(tempfile.gettempdir()).rglob("*")))
    for argument in argv[1:]:
        # Explicit system-prompt-file arguments may live outside the worktree.
        if "\n" not in argument and len(argument) < 4096:
            path = Path(argument)
            candidates.add(path if path.is_absolute() else worktree / path)
    contents = []
    for path in candidates:
        relative = path.relative_to(worktree).as_posix() if path.is_relative_to(worktree) else None
        if path not in automatic and str(path) not in references and not (relative and relative in references):
            continue
        try:
            if path.is_file():
                contents.append(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            continue
    return contents


class Commands:
    """Fake only process I/O; run the actual executor, runner and state store."""

    def __init__(self, case, monkeypatch, *, error=None, exit_code=0, is_error=True):
        from foragent import executors

        self.case, self.calls = case, []
        self.error, self.exit_code, self.is_error = error, exit_code, is_error
        monkeypatch.setattr(executors, "run_logged", self.run)
        case.ctx.executor_factory = lambda name: (
            executors.executor_for(name) if name == case.config.executor else case.reviewer
        )
        case.ctx.poll_hook = lambda execute, poll: execute()

    def run(self, argv, **kwargs):
        argv = list(argv)
        assert Path(argv[0]).name == self.case.config.executor, argv
        worktree = Path(kwargs["cwd"])
        messages = input_messages(argv, kwargs.get("prompt"))
        self.calls.append({
            "argv": argv, "stdin": kwargs.get("prompt"), "messages": messages,
            "files": delivered_files(self.case, argv, messages, worktree),
        })
        if self.error is not None:
            # The CLI can exit 0 and even mark a synthetic slash-command reply
            # is_error=false despite never starting an implementation turn.
            events = [
                {"type": "assistant", "message": {
                    "role": "assistant", "model": "<synthetic>",
                    "content": [{"type": "text", "text": self.error}],
                }},
                {"type": "result", "subtype": "error_during_execution" if self.is_error else "success",
                 "is_error": self.is_error,
                 "num_turns": 0, "total_cost_usd": 0, "result": self.error,
                 "modelUsage": {"<synthetic>": {"costUSD": 0}}},
            ]
        else:
            self.case.implement({"worktree": worktree})
            if self.case.config.executor == "codex":
                events = [{"type": "thread.started", "thread_id": "offline-goal-codex"},
                          {"type": "turn.completed"}]
                Path(argv[argv.index("-o") + 1]).write_text("Implemented", encoding="utf-8")
            else:
                events = [{"type": "result", "subtype": "success", "is_error": False,
                           "num_turns": 1, "total_cost_usd": 0.01, "result": "Implemented"}]
        output = "\n".join(json.dumps(event) for event in events) + "\n"
        stdout = Path(kwargs["stdout_path"])
        stdout.parent.mkdir(parents=True, exist_ok=True)
        stdout.write_text(output, encoding="utf-8")
        log = Path(kwargs["log_path"])
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as stream:
            stream.write(output)
        return self.exit_code, False, output


def goal_case(git_history, tmp_path, monkeypatch, *, executor="claude"):
    reviewer = "codex" if executor == "claude" else "claude"
    case = make_case(git_history, tmp_path, monkeypatch, executor=executor, reviewer=reviewer,
                     skill_libraries=[], test_command="true", max_retries=2)
    if reviewer == "claude":
        case.reviewer.steps = [json.dumps({"result": "(none)"})]
    return case


def test_long_claude_goal_separates_full_prompt_and_preserves_normal_codex_argv(
    git_history, tmp_path, monkeypatch,
):
    from foragent.issue import body_hash
    from foragent.runner import run_issue

    # Keep the already-green Codex regression guard inside this failing Claude
    # regression, so every collected test exposes a current contract violation.
    codex = goal_case(git_history, tmp_path / "codex", monkeypatch, executor="codex")
    codex_commands = Commands(codex, monkeypatch)
    assert run_issue(1, codex.ctx) == "merged"
    assert len(codex_commands.calls) == 1
    argv = codex_commands.calls[0]["argv"]
    assert argv[:10] == [
        "codex", "exec", "-", "-C", str(codex.store.worktree_path(1)),
        "-s", "workspace-write", "--skip-git-repo-check", "--json", "-o",
    ]
    assert Path(argv[10]).name == "last-message.txt"
    assert argv[11:] == ["-c", "model_reasoning_effort=high"]
    assert not codex_commands.calls[0]["stdin"].lstrip().startswith("/goal")
    assert "Implement the feature." in codex_commands.calls[0]["stdin"]

    case = goal_case(git_history, tmp_path / "claude", monkeypatch)
    scenarios = [
        ("unsaved edits", "retain pending settings without overwriting the saved version"),
        ("duplicate names", "resolve the workspace by its stable identifier instead of its display name"),
        ("missing target", "report the unavailable target and preserve the original selection"),
        ("offline sync", "complete the local migration and retain queued synchronization work"),
        ("read-only source", "copy the available settings without modifying the original source"),
        ("interrupted copy", "resume from the last complete checkpoint without duplicating entries"),
        ("changed default", "restore the user's saved selection instead of choosing the new default"),
        ("non-ASCII paths", "preserve the workspace path and display name exactly as supplied"),
    ]
    goal = "Implement feature.txt for the approved workspace migration.\n" + "\n".join(
        f"Source: {source}; workspace: {workspace}; state: {state}. The migration must {expected}. "
        "Report the completed migration and preserve all unrelated workspace settings."
        for source in ("local profile", "synced profile", "imported settings", "restored backup")
        for workspace in ("personal", "team", "archived", "연구 프로젝트")
        for state, expected in scenarios
    ) + "\nThe final migration must keep the user's selected workspace."
    assert len(goal) > 20000
    approval = case.store.load_approval(1)
    body = approval.body.replace("Implement the feature.", goal)
    case.tracker.update_issue(1, title="Implement workspace migration", body=body)
    case.store.save_approval(**{**asdict(approval), "body": body, "body_sha": body_hash(body)})
    commands = Commands(case, monkeypatch)
    assert run_issue(1, case.ctx) == "merged"
    assert len(commands.calls) == 1
    call = commands.calls[0]
    goal_messages = [text.lstrip() for text in call["messages"] if text.lstrip().startswith("/goal ")]
    assert len(goal_messages) == 1, "Claude must still receive one native /goal condition"
    condition = goal_messages[0][len("/goal "):]
    assert 0 < len(condition) <= 4000, (
        f"Claude receives {len(condition)} characters as its /goal condition; "
        "the whole message after /goal counts, including text after blank lines"
    )
    assert "Done when" in condition
    assert case.config.acceptance_dir in condition
    outside_goal = "\n".join([
        *(text for text in call["messages"] if not text.lstrip().startswith("/goal ")),
        *call["files"],
    ])
    assert goal in outside_goal, "Claude must receive the entire issue Goal outside /goal"
    assert "Other features." in outside_goal
    assert "test -f feature.txt" in outside_goal
    assert "test -f tests/acceptance/test_feature.py" in outside_goal
    assert ".foragent/TRY.md" in outside_goal, "Keep the full implementer instructions"
    assert goal not in condition
    assert "You are implementing GitHub issue" not in condition


@pytest.mark.parametrize("exit_code,is_error,error", [
    (0, False, GOAL_ERROR),
    (0, True, GOAL_ERROR),
    (1, True, "Cannot start the implementation: the project directory is unavailable."),
], ids=["synthetic-success-envelope", "zero-exit-error", "different-startup-error"])
def test_zero_turn_claude_error_stops_once_without_spending_attempts_and_keeps_detail(
    git_history, tmp_path, monkeypatch, exit_code, is_error, error,
):
    from foragent.runner import run_issue
    from foragent.state import StateStore

    case = goal_case(git_history, tmp_path, monkeypatch)
    initial_attempt = case.store.get_run(1).attempts
    commands = Commands(case, monkeypatch, error=error, exit_code=exit_code, is_error=is_error)
    outcome = run_issue(1, case.ctx)
    store = StateStore(case.store.root)
    run = store.get_run(1)
    history = [past for past in store.read_history() if past.number == 1]
    flags = [comment for comment in case.tracker.list_comments(1)
             if comment.body.startswith("<!-- foragent:blocked reason=")]
    # attempts is a one-based current-attempt ordinal. A failed executor launch
    # must leave that ordinal unchanged rather than advancing through the budget.
    observed = {
        "executor_calls": len(commands.calls),
        "attempts_advanced": run.attempts - initial_attempt,
        "finished": run.status == "finished" and run.outcome == outcome,
        "executor_error_in_detail": error in run.detail,
        "history_entries": len(history),
        "executor_error_in_history": len(history) == 1 and error in history[0].detail,
        "orchestrator_flags": len(flags),
        "executor_error_in_flag": len(flags) == 1 and error in flags[0].body,
        "blocked_for_orchestrator": store.load_flag(1) is not None
            and "blocked" in case.tracker.get_issue(1).labels,
        "reviewer_calls": len(case.reviewer.calls),
        "published_prs": len(case.tracker.created_prs),
    }
    assert observed == {
        "executor_calls": 1,
        "attempts_advanced": 0,
        "finished": True,
        "executor_error_in_detail": True,
        "history_entries": 1,
        "executor_error_in_history": True,
        "orchestrator_flags": 1,
        "executor_error_in_flag": True,
        "blocked_for_orchestrator": True,
        "reviewer_calls": 0,
        "published_prs": 0,
    }, f"A zero-turn executor refusal must stop immediately and retain its reason: {observed}"
