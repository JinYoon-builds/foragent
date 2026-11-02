"""Role-scoped default skills: one prompt section built from `default_skills` (#1619).

The caller names the role; an empty or unset role adds nothing and never borrows another role's list.
The section names each skill, its description and its SKILL.md path, so the session reads a skill
only when it fits the work. Wording lives in foragent.messages.skills_used.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from foragent.config import DEFAULT_SKILL_ROLES
from foragent.i18n import text
from foragent.skills import (
    OPEN_SOURCE_LICENSES, Skill, SkillError, SkillManager, copy_license, entry_directory, skill_summary,
    with_vendored_library,
)

if TYPE_CHECKING:
    from foragent.config import Config

SECTION_BYTES = 8 * 1024


def role_entries(config: Config, role: str) -> list[str]:
    """The role's assigned skills in the written order, once each."""
    return list(dict.fromkeys((config.default_skills or {}).get(role) or []))


def assigned_entries(config: Config) -> list[str]:
    """Every role's assigned skills, once each."""
    return list(dict.fromkeys(entry for role in DEFAULT_SKILL_ROLES for entry in role_entries(config, role)))


def assigned_names(config: Config) -> set[str]:
    return {entry_directory(entry) for entry in assigned_entries(config)}


class _Finder:
    """Find a skill's file: this checkout, then the project's own copy, then a library cache.

    Every copy answers for its own license facts, wherever it is found, so only a
    redistributable copy reaches a session.
    """

    def __init__(self, config: Config, root: Path, worktree: Path):
        self.config, self.root, self.worktree = config, root, worktree
        self._library = None

    def _local(self, directory: str, origin: Skill | None) -> tuple[Path, str] | None:
        for base, relative in ((self.worktree, True), (self.root, False)):
            for folder in (f".claude/skills/{directory}", f"skills/{directory}"):
                path = base / folder / "SKILL.md"
                if path.is_file() and copy_license(base / folder, base, origin) in OPEN_SOURCE_LICENSES:
                    return path, f"{folder}/SKILL.md" if relative else str(path.absolute())
        return None

    def _select(self, entry: str) -> Skill | None:
        try:
            if self._library is None:
                self._library = SkillManager(with_vendored_library(self.config, self.root))
                self._skills = self._library.skills(available_only=True)
            return self._library._select(entry, skills=self._skills)
        except (SkillError, OSError):
            return None

    def find(self, entry: str) -> tuple[Path, str] | None:
        """The file to read and the path to show, or None when no permitted copy is reachable."""
        skill = self._select(entry)
        directory = skill.path.name if skill is not None else entry_directory(entry)
        if (found := self._local(directory, skill)) is not None:
            return found
        if skill is None or skill.license not in OPEN_SOURCE_LICENSES:
            return None
        path = (skill.path / "SKILL.md").resolve()
        return path, str(path)


def _cut(description: str, limit: int) -> str:
    data = description.encode("utf-8")
    if len(data) <= limit:
        return description
    return data[:max(limit - 3, 0)].decode("utf-8", errors="ignore").rstrip() + "..." if limit > 3 else ""


def section(config: Config, role: str, root: Path, worktree: Path | None = None) -> str:
    """The role's prompt section, or "" when the role has no default skills.

    Descriptions share what names and paths leave of SECTION_BYTES, so twelve defaults stay
    within it however long their descriptions are, and every assigned skill stays listed.
    """
    entries = role_entries(config, role)
    if not entries:
        return ""
    finder = _Finder(config, root, worktree or root)
    rows: list[tuple[str, str, str | None]] = []
    for entry in entries:
        found = finder.find(entry)
        if found is None:
            rows.append((entry, "", None))
            continue
        try:
            name, description = skill_summary(found[0])
        except SkillError:
            name, description = entry_directory(entry), ""
        rows.append((name, " ".join(description.split()), found[1]))
    head = [text("default_skills_heading"), text("default_skills_intro")]

    def item(name: str, description: str, path: str | None) -> str:
        if path is None:
            return text("default_skills_missing", name=name)
        if not description:
            return text("default_skills_bare_item", name=name, path=path)
        return text("default_skills_item", name=name, description=description, path=path)

    fixed = "\n".join([*head, *(item(name, "", path) for name, _, path in rows)])
    # The final line end, the two leading ones, and ": " each description adds to its bare line.
    described = sum(1 for _, description, path in rows if description and path)
    share = max(0, SECTION_BYTES - len(fixed.encode("utf-8")) - 3) // described - 2 if described else 0
    lines = [item(name, _cut(description, share), path) for name, description, path in rows]
    return "\n\n" + "\n".join([*head, *lines]) + "\n"
