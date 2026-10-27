"""The default role agents (orchestrator, architect, docs) that fa init gives every project.

Projects differ only in memory and project skills: the agent definitions come
from the template below, and the role skills are copied from the ones Foragent
ships. Existing files are user-owned and never rewritten.
"""

from pathlib import Path
import re
import sysconfig

import yaml

from foragent.paths import repo_path


ROLES = {"fa-orchestrator": "orchestrator", "fa-architect": "architect", "fa-docs": "docs"}
MEMORY = {"fa-docs": "# fa-docs memory\n\n## Proposals\n\n## Lessons\n"}
_CHECKOUT_SKILLS = Path(__file__).resolve().parents[2] / "skills"


def agent_text(role: str) -> str:
    title = ROLES[role]
    return (
        f"---\nname: {role}\ndescription: Foragent {title} role\nmemory: project\nskills:\n- {role}\n---\n"
        f"You are this repository's Foragent {title}; follow the {role} skill.\n"
        "Record durable, project-specific lessons (patterns, pitfalls, commands) in your memory.\n"
    )


def skill_source(role: str) -> Path | None:
    """The shipped role skill: a source checkout, else the installed package data."""
    for base in (_CHECKOUT_SKILLS, Path(sysconfig.get_path("data")) / "share/foragent/skills"):
        if (base / role / "SKILL.md").is_file():
            return base / role
    return None


def _valid_agent(path: Path, role: str) -> bool:
    try:
        content = path.read_text(encoding="utf-8")
        match = re.match(r"\A---[ \t]*\n(.*?)^---[ \t]*(?:\n|$)", content, re.S | re.M)
        header = yaml.safe_load(match[1]) if match else None
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return False
    return (isinstance(header, dict) and header.get("name") == role
            and header.get("memory") == "project" and header.get("skills") == [role])


def _templates(root: Path, *, existing_skills: bool = False) -> dict[str, str]:
    """What init writes for the roles, keyed by repository path.

    An existing skill directory is kept whole, so its files are listed only on request.
    """
    contents: dict[str, str] = {}
    for role in ROLES:
        contents[f".claude/agents/{role}.md"] = agent_text(role)
        contents[f".claude/agent-memory/{role}/MEMORY.md"] = MEMORY.get(role, f"# {role} memory\n")
        skill = f".claude/skills/{role}"
        source = skill_source(role)
        if source is not None and (existing_skills or not repo_path(root, skill).exists()):
            for file in sorted(source.rglob("*")):
                if file.is_file() and not file.is_symlink():
                    contents[f"{skill}/{file.relative_to(source).as_posix()}"] = file.read_text(encoding="utf-8")
    return contents


def missing_files(root: Path) -> tuple[dict[str, str], list[str]]:
    """Files to create (relative path -> text) and agent files left alone with bad frontmatter."""
    contents = {path: text for path, text in _templates(root).items() if not repo_path(root, path).exists()}
    invalid = [f".claude/agents/{role}.md" for role in ROLES
               if repo_path(root, f".claude/agents/{role}.md").exists()
               and not _valid_agent(repo_path(root, f".claude/agents/{role}.md"), role)]
    return contents, invalid


def setup_paths(root: Path) -> list[str]:
    """Role paths an interrupted init may have left uncommitted: its own, unedited output."""
    owned = []
    for path, text in _templates(root, existing_skills=True).items():
        target = repo_path(root, path)
        if target.is_file() and not target.is_symlink() and target.read_bytes() == text.encode("utf-8"):
            owned.append(path)
    return owned
