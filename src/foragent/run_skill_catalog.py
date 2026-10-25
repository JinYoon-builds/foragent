"""Per-issue skill catalog for the implementer, and its `Skills used:` report.

Code gathers the facts (which approved skills exist, their descriptions and
paths); the lane agent chooses which ones fit the issue and reports them.
Wording lives in foragent.messages.skills_per_issue.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from foragent.i18n import text
from foragent.skills import SkillError, SkillManager

if TYPE_CHECKING:
    from foragent.config import Config


def available_skills(config: Config, fixed: list[str]) -> list[tuple[str, str, str]]:
    """Approved non-style skills that are present locally, minus the lane's fixed skills.

    "Approved" means a person confirmed a role for the skill (`skill_roles`).
    Style skills stay a person's per-lane choice, so they are never offered.
    """
    try:
        skills = SkillManager(config).skills(available_only=True)
    except (SkillError, OSError):
        return []
    fixed_names = set(fixed)
    offered, seen = [], set()
    for skill in skills:
        if skill.role in (None, "style") or skill.name in fixed_names or skill.path.name in fixed_names:
            continue
        if skill.id in seen:
            continue
        seen.add(skill.id)
        description = " ".join(skill.description.split())
        offered.append((skill.name, description, str((skill.path / "SKILL.md").resolve())))
    return offered


def catalog_section(skills: list[tuple[str, str, str]]) -> str:
    """The prompt section; it always asks for the report so every run states its skills."""
    lines = ["", "", text("skills_catalog_heading")]
    if skills:
        lines.append(text("skills_catalog_intro"))
        lines.extend(text("skills_catalog_item", name=name, description=description, path=path)
                     for name, description, path in skills)
    else:
        lines.append(text("skills_catalog_empty"))
    lines.append(text("skills_catalog_report"))
    return "\n".join(lines) + "\n"


_REPORT = re.compile(r"[\s>*_`-]*Skills used:\s*(.*?)[\s`*_.]*", re.IGNORECASE)


def parse_skills_used(message: str) -> list[str] | None:
    """The names in the last `Skills used:` line; None when the agent did not report."""
    for line in reversed(message.splitlines()):
        match = _REPORT.fullmatch(line)
        if match is None:
            continue
        names = [name.strip(" `*_\"'") for name in match[1].split(",")]
        names = [name for name in names if name]
        if len(names) == 1 and names[0].casefold() == "none":
            return []
        return list(dict.fromkeys(names))
    return None


def report_line(used: list[str], reported: bool) -> str:
    if not reported:
        names = text("skills_used_not_reported")
    else:
        names = ", ".join(used) if used else text("skills_used_none")
    return text("skills_used_line", names=names)
