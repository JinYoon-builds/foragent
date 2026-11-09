"""Guard (passes on main, outside Done when): after fa init, writing a file that is not
*/memory/*.md never gets context added by any PreToolUse hook in the planning session's
Claude Code settings, and no hook blocks the write (Jin r1315, statement 3 regression control).
Also: without the planning-session hooks consent (--decision-hooks no) init writes no
reminder hook, whatever the other consents (Claude settings only with consent, PM answer).
"""
import pytest

from planning_memory_hook_case import COMMANDS, Case, WRITE_TOOLS


@pytest.fixture
def case(tmp_path, monkeypatch):
    return Case(tmp_path, monkeypatch)


@pytest.mark.parametrize("tool_name", WRITE_TOOLS)
def test_other_file_writes_get_no_added_context(case, tool_name):
    case.init()
    words = "Decision: ship on Fridays. If the night run fails then page Jin. Order idea: a new lane."
    for path in (case.root / "README.md",
                 case.root / "docs" / "memory.md",
                 case.root / "notes" / "memory" / "plan.txt",
                 case.root / "src" / "memory" / "store.py"):
        assert case.contexts(path, tool_name, content=words) == [], f"No context for {path}"


@pytest.mark.parametrize("others", ("yes", "no"))
def test_no_reminder_without_the_planning_session_hooks_consent(case, others):
    case.init(decision_hooks="no", others=others)
    for tool_name in WRITE_TOOLS:
        added = case.contexts(case.memory_dir / "MEMORY.md", tool_name)
        assert not [t for t in added if any(c in t for c in COMMANDS)], (
            f"--decision-hooks no installs no memory-write reminder: {added}")
    assert case.decision_hook_commands() == [], "and no decision hook either"
