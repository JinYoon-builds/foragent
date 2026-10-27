"""Acceptance test for init-role-agents. Locked by Foragent; do not edit."""

from pathlib import Path
import subprocess
import time

import pytest

from test_init import setup
from test_role_agents import ROOT, ROLES, assert_role_agent, frontmatter
from test_skills_autosync import cli_exit, isolated_home


@pytest.fixture
def init_project(setup, isolated_home, monkeypatch):
    """Run real init via the CLI, with setup's offline Git/GitHub dependencies."""
    from foragent import cli
    from foragent.git import GitRepo

    def forbidden(*args, **kwargs):
        pytest.fail("Init role tests must not publish or sleep")

    commits = []
    monkeypatch.setattr(GitRepo, "commit_paths", lambda self, *args, **kwargs: commits.append(args))
    monkeypatch.setattr(GitRepo, "push_base", forbidden)
    monkeypatch.setattr(time, "sleep", forbidden)
    monkeypatch.setattr(cli, "init_repo", lambda root, **kwargs: setup.init(**kwargs))
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    popen = subprocess.Popen

    def local_git_only(argv, *args, **kwargs):
        # Tool-auth probes are already faked by setup; nothing external may start.
        assert isinstance(argv, (list, tuple)) and argv and argv[0] == "git", (
            f"Init acceptance tests permit only local Git subprocesses: {argv}"
        )
        assert not {"commit", "push", "reset", "checkout", "stash", "clone", "fetch", "pull", "ls-remote"}.intersection(argv), (
            f"Init role tests must not mutate Git history or access a remote: {argv}"
        )
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", local_git_only)
    argv = ["init", "--repo-root", str(setup.root), "--approver", "human",
            "--no-push", "--no-launchd", "--no-board", "--statusline", "no"]

    def run():
        assert cli_exit(cli.main, argv) == 0, "fa init must succeed with offline setup dependencies"

    return setup.root, Path.home(), run


@pytest.mark.parametrize("role", ROLES)
def test_init_role_agent_and_memory(init_project, role):
    root, _, run = init_project
    assert not (root / ".foragent.yml").exists(), "Start with a fresh project"
    run()
    assert_role_agent(root, role)
    relative = f".claude/agent-memory/{role}/MEMORY.md"
    assert (root / relative).is_file(), f"fa init must create {relative}"


@pytest.mark.parametrize("role", ROLES)
def test_init_role_skill_available(init_project, role):
    from foragent.config import load_config
    from foragent.skills import SkillManager

    root, home, run = init_project
    run()
    # Accept project-local installation or the existing user-level skill links,
    # as well as an already available local library. Never sync a remote library.
    candidates = [base / relative / role / "SKILL.md" for base in (root, home)
                  for relative in (".claude/skills", ".agents/skills", ".codex/skills")]
    candidates.append(root / "skills" / role / "SKILL.md")
    manager = SkillManager(load_config(root))
    candidates.extend(skill.path / "SKILL.md" for skill in manager.skills(available_only=True) if skill.name == role)
    available = [path for path in candidates if path.is_file()]
    assert available, f"fa init must make the {role} skill available to this project"
    source = ROOT / "skills" / role / "SKILL.md"
    assert source.is_file(), f"Ship the built-in {role} skill"
    expected = source.read_text(encoding="utf-8").strip()
    assert any(path.read_text(encoding="utf-8").strip() == expected for path in available), (
        f"Install or link the shipped {role} skill, including its procedure"
    )
    for path in available:
        metadata, _ = frontmatter(path)
        assert metadata.get("name") == role, f"Installed {role} skill must keep its frontmatter name"


@pytest.mark.parametrize("kept,missing", [(ROLES[0], ROLES[1]), (ROLES[1], ROLES[0])],
                         ids=["keep-orchestrator", "keep-architect"])
def test_reinit_preserves_edits_and_adds_missing_roles(init_project, kept, missing):
    root, _, run = init_project
    run()
    agent = root / ".claude/agents" / f"{kept}.md"
    memory = root / ".claude/agent-memory" / kept / "MEMORY.md"
    # These are user-owned files, also when adopting an existing project that
    # supplied its role definition before Foragent supported roles.
    edited_agent = (f"---\nname: {kept}\nmemory: project\nskills: [{kept}]\n---\n"
                    "Keep this project's edited role body verbatim.\n프로젝트별 판단 맥락.\n")
    remembered = "# Project memory\nrequest id: req-7; date: 2026-10-27; expected effect: 12 waits -> 3\n"
    agent.parent.mkdir(parents=True, exist_ok=True)
    memory.parent.mkdir(parents=True, exist_ok=True)
    agent.write_text(edited_agent, encoding="utf-8")
    memory.write_text(remembered, encoding="utf-8")
    before_agent, before_memory = agent.read_bytes(), memory.read_bytes()

    missing_agent = root / ".claude/agents" / f"{missing}.md"
    missing_memory = root / ".claude/agent-memory" / missing / "MEMORY.md"
    missing_agent.unlink(missing_ok=True)
    missing_memory.unlink(missing_ok=True)
    run()
    assert agent.read_bytes() == before_agent, f"Re-running init must preserve the edited {kept} agent body"
    assert memory.read_bytes() == before_memory, f"Re-running init must preserve existing {kept} project memory"
    assert_role_agent(root, missing)
    assert missing_memory.is_file(), f"Re-running init must add missing {missing} project memory"
