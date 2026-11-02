"""Per-issue skill catalog for the implementer, and its `Skills used:` report.

Code gathers the facts (which approved skills exist, their descriptions and
paths); the lane agent chooses which ones fit the issue and reports them.
Wording lives in foragent.messages.skills_used.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from foragent import cuts, session_skills
from foragent.i18n import text
from foragent.skills import OPEN_SOURCE_LICENSES, SkillError, SkillManager, trusted_source

if TYPE_CHECKING:
    from foragent.config import Config


def available_skills(config: Config, fixed: list[str]) -> list[tuple[str, str, str]]:
    """Approved non-style skills that are present locally, minus the lane's fixed skills.

    "Approved" means a person confirmed a role for the skill (`skill_roles`), or it comes from a
    trusted source under a license that allows redistribution. Style skills stay a person's
    per-lane choice, so they are never offered. A skill in `default_skills` reaches its roles
    through their own section, never through this catalog.
    """
    try:
        skills = SkillManager(config).skills(available_only=True)
    except (SkillError, OSError):
        return []
    held_back = {*fixed, *session_skills.assigned_names(config)}
    offered, seen = [], set()
    for skill in skills:
        if skill.role == "style" or skill.name in held_back or skill.path.name in held_back:
            continue
        if skill.role is None and not (trusted_source(skill.library.repo) and skill.license in OPEN_SOURCE_LICENSES):
            continue
        if skill.id in seen:
            continue
        seen.add(skill.id)
        description = " ".join(skill.description.split())
        offered.append((skill.name, description, str((skill.path / "SKILL.md").resolve())))
    return offered


# A longer catalog moves to a file, so about 270 installed skills leave a small issue's prompt small.
CATALOG_INLINE_BYTES = 6 * 1024


def catalog_section(skills: list[tuple[str, str, str]], full_text_dir: Path | None = None) -> str:
    """The prompt section; it always asks for the report so every run states its skills."""
    lines = ["", "", text("skills_catalog_heading")]
    items = [text("skills_catalog_item", name=name, description=description, path=path)
             for name, description, path in skills]
    if full_text_dir is not None and len("\n".join(items).encode("utf-8")) > CATALOG_INLINE_BYTES:
        catalog = "\n".join([text("skills_catalog_heading"), text("skills_catalog_intro"), *items]) + "\n"
        path = cuts.save_full(full_text_dir, "available-skills.md", catalog)
        lines.append(text("skills_catalog_file", count=len(items), path=path))
    elif skills:
        lines.append(text("skills_catalog_intro"))
        lines.extend(items)
    else:
        lines.append(text("skills_catalog_empty"))
    lines.append(text("skills_catalog_report"))
    return "\n".join(lines) + "\n"


# The report prefix comes from the same template the PR line uses.
_PREFIX = text("skills_used_line", names="").strip()
_REPORT = re.compile(r"[\s>*_`-]*" + re.escape(_PREFIX) + r"\s*(.*?)[\s`*_.]*", re.IGNORECASE)


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
