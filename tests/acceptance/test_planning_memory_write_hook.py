"""Acceptance: fa init installs a memory-write reminder hook for planning sessions (Jin r1315).

Statements:
1. After fa init with the planning-session hooks consent (--decision-hooks yes), the planning
   session's Claude Code settings contain a hook that runs before file writes (a PreToolUse
   hook matching the Write and Edit tools), next to the existing decision hooks: in the same
   project .claude/settings.local.json that holds `fa decisions stop-check` and
   `fa decisions _sweep`.
2. Given a write to a path matching */memory/*.md, that hook's output tells the session to use
   `fa order note --kind decision`, `fa orch promise add` and `fa order note --kind idea`.
3. Given a write to any other path, that hook prints nothing that adds context.

"Tells the session" is Claude Code's PreToolUse hook output
`{"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": ...}}`; the hook
exits 0 (a reminder, not a block). The hook judges the act of writing a memory file only,
never the words. Regression controls that pass on main live in
test_planning_memory_write_hook_guard.py.
"""
import pytest

from planning_memory_hook_case import COMMANDS, Case, WRITE_TOOLS, additional_context


@pytest.fixture
def case(tmp_path, monkeypatch):
    return Case(tmp_path, monkeypatch)


def reminders(case, path, tool_name="Write", content="Short note."):
    return [text for text in case.contexts(path, tool_name, content)
            if all(command in text for command in COMMANDS)]


@pytest.mark.parametrize("others", ("yes", "no"))
@pytest.mark.parametrize("tool_name", WRITE_TOOLS)
def test_init_installs_a_hook_that_runs_before_file_writes(case, tool_name, others):
    # Only the planning-session hooks consent is needed; the other offers may be declined.
    case.init(decision_hooks="yes", others=others)
    hooks = case.pre_tool_use_hooks(tool_name)
    assert hooks, (f"After fa init --decision-hooks yes the planning session's Claude Code settings "
                   f"carry a PreToolUse hook for {tool_name}; found none in {[str(p) for p in case.settings]}")
    memory_file = case.memory_dir / "MEMORY.md"
    assert len(reminders(case, memory_file, tool_name)) == 1, (
        "Exactly one installed hook reminds the session when it is about to write its memory file")
    local = case.root / ".claude/settings.local.json"
    reminding = [command for path, command in hooks
                 if path == local and len(reminders_for(case, command, memory_file, tool_name)) == 1]
    assert reminding, (f"The reminder hook sits next to the existing decision hooks in {local}; "
                       f"hooks found: {hooks}")
    decisions = case.decision_hook_commands()
    assert any("stop-check" in c for c in decisions) and any("_sweep" in c for c in decisions), (
        f"The existing decision hooks are still installed beside it: {decisions}")


def reminders_for(case, command, path, tool_name):
    code, out, _err = case.run_hook(command, case.payload(path, tool_name))
    text = additional_context(out) if code == 0 else ""
    return [text] if text and all(c in text for c in COMMANDS) else []


@pytest.mark.parametrize("tool_name", WRITE_TOOLS)
def test_memory_file_write_reminds_the_three_fa_commands(case, tool_name):
    case.init()
    for path in (case.memory_dir / "MEMORY.md",
                 case.memory_dir / "project_decisions.md",
                 case.root / "notes" / "memory" / "plan.md"):
        added = case.contexts(path, tool_name)
        assert added, f"Writing {path} adds a reminder to the session's context"
        assert len(added) == 1, f"One reminder per memory write, not {len(added)}: {added}"
        text = added[0]
        for command in COMMANDS:
            assert command in text, f"The reminder names `{command}`: {text!r}"


def test_reminder_reacts_to_the_act_not_the_words(case):
    case.init()
    plain = case.memory_dir / "user_profile.md"
    added = case.contexts(plain, "Write", content="The person prefers short answers in Korean.")
    assert len(added) == 1 and all(command in added[0] for command in COMMANDS), (
        "A memory write with no decision words still gets the reminder")


def test_rerunning_init_keeps_a_single_reminder(case):
    case.init()
    case.init()
    assert len(reminders(case, case.memory_dir / "MEMORY.md")) == 1, (
        "A second fa init does not install the reminder twice")


@pytest.mark.parametrize("tool_name", WRITE_TOOLS)
def test_other_file_writes_add_no_context(case, tool_name):
    case.init()
    memory_file = case.memory_dir / "MEMORY.md"
    assert reminders(case, memory_file, tool_name), (
        "The installed hook is present and reminds on a memory write (control for this test)")
    decision_words = "Decision: we decided to ship on Fridays. If X then Y. Order idea: new lane."
    for path in (case.root / "README.md",
                 case.root / "docs" / "memory.md",
                 case.root / "notes" / "memory" / "plan.txt",
                 case.root / "src" / "memory" / "store.py",
                 case.home / ".claude" / "projects" / "MEMORY.md",
                 case.transcript):
        assert case.contexts(path, tool_name, content=decision_words) == [], (
            f"Writing {path} (not */memory/*.md) adds no context, whatever the words")
