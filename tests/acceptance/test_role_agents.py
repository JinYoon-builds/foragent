"""Acceptance test for role-agents. Locked by Foragent; do not edit."""

import json
from pathlib import Path
import shutil
import time

import pytest
import yaml

from test_orch_architect import WHY, architect_case, call_architect, finish_session
from test_skills_autosync import isolated_home


ROOT = Path(__file__).resolve().parents[2]
ROLES = ("fa-orchestrator", "fa-architect")
ARCHITECT_SKILL = Path("skills/fa-architect/SKILL.md")
ARCHITECT_MEMORY = ".claude/agent-memory/fa-architect/MEMORY.md"
PROCEDURE_PHRASES = (
    "one proposal per file or concern",
    "compatibility facade",
    "one writer per piece of state",
    "derive instead of store",
    "append instead of overwrite",
)


def normalized(text):
    return " ".join(text.casefold().replace("`", "").replace('"', "").split())


def frontmatter(path):
    assert path.is_file(), f"Missing role file: {path.as_posix()}"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0] == "---", f"{path.name}: YAML frontmatter is required"
    assert "---" in lines[1:], f"{path.name}: close the YAML frontmatter"
    end = lines.index("---", 1)
    metadata = yaml.safe_load("\n".join(lines[1:end]))
    assert isinstance(metadata, dict), f"{path.name}: frontmatter must be a mapping"
    return metadata, "\n".join(lines[end + 1:]).strip("\n")


def assert_role_agent(root, role):
    path = root / ".claude/agents" / f"{role}.md"
    assert path.is_file(), f"Missing role agent: .claude/agents/{role}.md"
    metadata, body = frontmatter(path)
    assert metadata.get("name") == role, f"{role}: frontmatter name must match the role"
    assert metadata.get("memory") == "project", f"{role}: use project memory"
    assert metadata.get("skills") == [role], f"{role}: attach its own role skill"
    assert len(body.splitlines()) <= 15, f"{role}: keep the agent body at most 15 lines; put procedure in skills"


@pytest.fixture
def architect_session(tmp_path, monkeypatch, capsys):
    """Reuse the worker fake, retaining real executor argv/prompt construction."""
    from foragent import executors

    def launch(kind):
        executor_class = {"claude": executors.ClaudeExecutor, "codex": executors.CodexExecutor}[kind]
        real_run = executor_class.run
        case = architect_case(tmp_path, monkeypatch)
        config = case.root / ".foragent.yml"
        config.write_text(config.read_text(encoding="utf-8") + f"executor: {kind}\n", encoding="utf-8")

        # Supply the shipped files to this separate project, without inventing
        # missing role instructions or letting the fake supply prompt content.
        source = ROOT / ARCHITECT_SKILL.parent
        if source.is_dir():
            for relative in (ARCHITECT_SKILL.parent, Path(".claude/skills/fa-architect")):
                shutil.copytree(source, case.root / relative, dirs_exist_ok=True)
        for relative in (".claude/agents/fa-architect.md", ARCHITECT_MEMORY):
            source_file = ROOT / relative
            if source_file.is_file():
                destination = case.root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_file, destination)

        def run_logged(argv, *, cwd, prompt, stdout_path, **kwargs):
            assert Path(argv[0]).name == kind, f"Unexpected executor command: {argv}"
            assert cwd == case.root, "Run the architect in its project"
            case.turns.append({"argv": list(argv), "prompt": prompt})
            if kind == "claude":
                events = [{"type": "result", "result": "Nothing to change.", "num_turns": 1}]
            else:
                events = [{"type": "thread.started", "thread_id": "offline-architect"},
                          {"type": "turn.completed"}]
                Path(argv[argv.index("-o") + 1]).write_text("Nothing to change.", encoding="utf-8")
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            stdout_path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
            return 0, False, ""

        def no_sleep(*args, **kwargs):
            pytest.fail("Architect acceptance tests must not sleep")

        monkeypatch.setattr(executor_class, "run", real_run)
        monkeypatch.setattr(executors, "executor_for", lambda name: executor_class() if name == kind else
                            pytest.fail(f"Unexpected executor: {name}"))
        monkeypatch.setattr(executors, "run_logged", run_logged)
        monkeypatch.setattr(time, "sleep", no_sleep)
        child = call_architect(case, capsys, why=WHY)
        finish_session(case, child)
        assert case.gh_calls == [] and case.pushes == [], "Role sessions stay offline"
        return case.turns[0]

    return launch


@pytest.mark.parametrize("role", ROLES)
def test_repository_role_agent(role):
    assert_role_agent(ROOT, role)


def test_architect_skill_procedure():
    assert (ROOT / ARCHITECT_SKILL).is_file(), f"Missing architect skill: {ARCHITECT_SKILL}"
    metadata, body = frontmatter(ROOT / ARCHITECT_SKILL)
    assert metadata.get("name") == "fa-architect", "The architect skill must be named fa-architect"
    procedure = normalized(body)
    phrases = (
        ("measure first",),
        ("fa orch facts",),
        ("one proposal per file or concern",),
        ("each proposal cites", "every proposal cites"),
        ("numbers or a real incident", "numbers or an incident"),
        ("expected measurable effect",),
        ("preserves behaviour through a compatibility facade", "preserves behavior through a compatibility facade"),
        ("nothing to change is a valid", "nothing to change is valid"),
        ("one writer per piece of state",),
        ("derive instead of store",),
        ("append instead of overwrite",),
    )
    for alternatives in phrases:
        assert any(phrase in procedure for phrase in alternatives), (
            f"fa-architect skill must carry this procedure phrase: {alternatives[0]}"
        )


def test_refactor_compatibility_pointer():
    path = ROOT / "skills/fa-orchestrator/references/refactor.md"
    assert path.is_file(), "Keep the old refactor reference so existing links work"
    assert "skills/fa-architect" in normalized(path.read_text(encoding="utf-8")), (
        "The old refactor reference must point to skills/fa-architect"
    )


@pytest.mark.parametrize("kind", ("claude", "codex"))
def test_architect_executor_loads_role_skill_once(architect_session, kind):
    turn = architect_session(kind)
    prompt, argv = turn["prompt"], turn["argv"]
    assert WHY in prompt, "Preserve the caller's --why evidence in the architect prompt"
    if kind == "claude":
        assert any(argv[index:index + 2] == ["--agent", "fa-architect"] for index in range(len(argv))), (
            "Claude architect launch must name --agent fa-architect"
        )
        assert "fa-architect" in prompt, "Point the Claude session to its architect role/skill"
        for phrase in PROCEDURE_PHRASES:
            assert phrase not in normalized(prompt), f"Keep the procedure in the Claude skill, not the prompt: {phrase}"
    else:
        assert "fa-architect" in prompt, "Codex architect prompt must include the fa-architect skill"
        skill = ROOT / ARCHITECT_SKILL
        assert skill.is_file(), "Codex must receive the shipped architect skill"
        skill_text = skill.read_text(encoding="utf-8").strip()
        assert skill_text in prompt, "Include the text of skills/fa-architect/SKILL.md for Codex"
        outside_skill = prompt.replace(skill_text, "", 1)
        for phrase in PROCEDURE_PHRASES:
            assert phrase not in normalized(outside_skill), f"Do not repeat the injected architect procedure: {phrase}"


@pytest.mark.parametrize("kind", ("claude", "codex"))
def test_architect_prompt_proposal_memory(architect_session, kind):
    prompt = normalized(architect_session(kind)["prompt"])
    assert ARCHITECT_MEMORY.casefold() in prompt, (
        "Architect prompt must record proposals in .claude/agent-memory/fa-architect/MEMORY.md"
    )
    for phrase in ("record", "each proposal", "request id", "date", "expected", "effect"):
        assert phrase in prompt, f"Architect proposal memory instructions must include: {phrase}"
    assert any(phrase in prompt for phrase in ("earlier proposals", "previous proposals", "past proposals")), (
        "Check earlier proposals rather than only recording new ones"
    )
    assert "measured effect" in prompt, "Check the measured effect of earlier proposals"
    assert any(command in prompt for command in ("fa orch scorecard", "fa orch facts")), (
        "Recheck earlier proposals with fa orch scorecard or fa orch facts"
    )


@pytest.mark.parametrize("role", ROLES)
def test_repository_role_memory(role):
    relative = f".claude/agent-memory/{role}/MEMORY.md"
    assert (ROOT / relative).is_file(), f"Missing role project memory: {relative}"
