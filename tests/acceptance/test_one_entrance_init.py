"""Real fa init, with existing offline setup adapters, creates the PM role."""

from test_init import setup  # noqa: F401


def test_new_project_has_pm_definition_and_empty_project_notebook(setup):
    setup.init(no_push=True, no_launchd=True)
    definition = setup.root / ".claude/agents/fa-pm.md"
    folder = setup.root / ".claude/agent-memory/fa-pm"
    assert definition.is_file(), "fa init must create .claude/agents/fa-pm.md"
    assert folder.is_dir() and (folder / "MEMORY.md").is_file(), "fa init must create the PM project notebook"
    assert "name: fa-pm" in definition.read_text() and "PM" in definition.read_text()
    assert not [line for line in (folder / "MEMORY.md").read_text().splitlines()
                if line.strip() and not line.lstrip().startswith("#")], "A new project starts without invented PM lessons"
    (folder / "MEMORY.md").write_text("# PM\nProject lesson kept across init.\n")
    (folder / "MEMORY.new.md").write_text("- [id:issue-4] #4 PM: Pending lesson.\n")
    before = definition.read_bytes(), (folder / "MEMORY.md").read_bytes(), (folder / "MEMORY.new.md").read_bytes()
    setup.init(no_push=True, no_launchd=True)
    assert before == (definition.read_bytes(), (folder / "MEMORY.md").read_bytes(), (folder / "MEMORY.new.md").read_bytes())
