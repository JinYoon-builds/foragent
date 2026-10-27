"""Acceptance test for claude-no-background. Locked by Foragent; do not edit."""

import pytest

# Evidence (2026-10-27, #712 and #466): a headless Claude executor started its tests
# as a background task and ended its turn; the session that woke for the task's
# notification produced nothing until the stall kill. Every Claude session the gate
# starts runs with background tasks disabled, so a long command stays in the turn.
SWITCH = "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"


@pytest.fixture
def launches(monkeypatch):
    from foragent import executors

    calls = []

    def fake_run_logged(argv, **kwargs):
        calls.append((argv, kwargs))
        for key in ("stdout_path",):
            if kwargs.get(key) is not None:
                kwargs[key].write_text("", encoding="utf-8")
        return 0, False, ""

    monkeypatch.setattr(executors, "run_logged", fake_run_logged)
    return calls


@pytest.mark.parametrize("options", (
    {"goal": "Done when the tests pass"},
    {},
    {"resume_session_id": "session-1"},
    {"read_only": True},
    {"review_level": "high", "review_pr": 7},
), ids=("goal-implementation", "plain", "resume", "read-only", "review"))
def test_every_claude_session_runs_without_background_tasks(launches, tmp_path, options):
    from foragent.executors import ClaudeExecutor

    ClaudeExecutor().run(worktree=tmp_path, prompt="Implement it", model=None,
                         timeout_s=60, log_path=tmp_path / "run.log", **options)
    assert len(launches) == 1
    argv, kwargs = launches[0]
    assert argv[0] == "claude"
    env = kwargs.get("env") or {}
    assert env.get(SWITCH) == "1", "A gate-started Claude session must not run background tasks"


def test_codex_sessions_are_unchanged(launches, tmp_path):
    from foragent.executors import CodexExecutor

    CodexExecutor().run(worktree=tmp_path, prompt="Implement it", model=None,
                        timeout_s=60, log_path=tmp_path / "run.log")
    argv, kwargs = launches[0]
    assert argv[0] == "codex"
    assert SWITCH not in (kwargs.get("env") or {}), "The switch is Claude Code's; Codex keeps its environment"
