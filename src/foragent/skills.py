"""Sync skill libraries, inspect their metadata, and install selected skills."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess

import yaml

from foragent.config import Config, load_global_config
from foragent.paths import repo_path


class SkillError(ValueError):
    """A library or installation cannot be used."""


@dataclass(frozen=True)
class Library:
    name: str
    repo: str
    path: Path


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    description: str
    library: Library
    path: Path


def _run(args: list[str]) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=300).stdout


class SkillManager:
    def __init__(self, config: Config | None = None, *, libraries_dir: Path | None = None,
                 run: Callable[[list[str]], str] | None = None):
        config = config or load_global_config()
        directory = libraries_dir or Path.home() / ".local/share/foragent/libraries"
        self.libraries = []
        self.run = run or _run
        for entry in config.skill_libraries:
            repo = entry if isinstance(entry, str) else entry["repo"]
            name = repo.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git") if isinstance(entry, str) else entry["name"]
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name in {".", ".."}:
                raise SkillError(f"invalid library name: {name}")
            if any(lib.name == name for lib in self.libraries):
                raise SkillError(f"duplicate library name: {name}")
            path = Path(entry["path"]).expanduser() if isinstance(entry, dict) and "path" in entry else directory / name
            self.libraries.append(Library(name, repo, path))

    def sync(self) -> list[str]:
        synced = []
        for library in self.libraries:
            if library.path.exists():
                self.run(["git", "-C", str(library.path), "pull", "--ff-only"])
            else:
                library.path.parent.mkdir(parents=True, exist_ok=True)
                self.run(["git", "clone", "--", library.repo, str(library.path)])
            synced.append(library.name)
        return synced

    def skills(self) -> list[Skill]:
        result = []
        for library in self.libraries:
            if not library.path.is_dir():
                raise SkillError(f"library {library.name} is missing; run fa skills sync")
            for path in sorted(library.path.glob("*/skills/*/SKILL.md")):
                match = re.match(r"\A---\s*\n(.*?)\n---(?:\s*\n|$)", path.read_text(encoding="utf-8"), re.S)
                try:
                    meta = yaml.safe_load(match[1]) if match else None
                except yaml.YAMLError as exc:
                    raise SkillError(f"invalid skill frontmatter: {path}: {exc}") from exc
                if not isinstance(meta, dict) or any(not isinstance(meta.get(key), str) for key in ("name", "description")):
                    raise SkillError(f"skill requires name and description frontmatter: {path}")
                plugin = path.relative_to(library.path).parts[0]
                identity = f"{plugin}/{path.parent.name}"
                if len(self.libraries) > 1:
                    identity = f"{library.name}/{identity}"
                result.append(Skill(identity, meta["name"], meta["description"], library, path.parent))
        return result

    def catalog(self) -> list[dict[str, str]]:
        return [{"id": skill.id, "name": skill.name, "description": skill.description} for skill in self.skills()]

    def _select(self, identity: str) -> Skill:
        for skill in self.skills():
            if identity in {skill.id, f"{skill.library.name}/{skill.id}"}:
                return skill
        raise SkillError(f"unknown skill: {identity}")

    def check_install(self, identity: str, root: Path) -> Skill:
        skill = self._select(identity)
        if skill.path.is_symlink() or any(path.is_symlink() for path in skill.path.rglob("*")):
            raise SkillError(f"skill contains a symlink: {identity}")
        destination = repo_path(root, f".claude/skills/{skill.path.name}")
        if destination.exists() and (destination.is_symlink() or not destination.is_dir()
                                     or self._contents(destination) != self._contents(skill.path)):
            raise SkillError(f"refusing to overwrite different existing skill: {destination}")
        repo_path(root, "THIRD_PARTY_NOTICES.md")
        return skill

    @staticmethod
    def _contents(path: Path) -> dict[str, bytes]:
        return {file.relative_to(path).as_posix(): file.read_bytes() for file in path.rglob("*") if file.is_file()}

    def install(self, identity: str, root: Path) -> str:
        skill = self.check_install(identity, root)
        sha = self.run(["git", "-C", str(skill.library.path), "rev-parse", "HEAD"]).strip()
        licenses = sorted(path for path in skill.library.path.iterdir()
                          if path.is_file() and path.name.upper().startswith(("LICENSE", "LICENCE", "COPYING")))
        license_file = licenses[0] if licenses else None
        license_text = license_file.read_text(encoding="utf-8") if license_file else ""
        spdx = re.search(r"SPDX-License-Identifier:\s*([^\s]+)", license_text)
        license_id = spdx[1] if spdx else (
            "Apache-2.0" if skill.library.name == "knowledge-work-plugins" or "Apache License" in license_text
            else "MIT" if "MIT License" in license_text else "not specified"
        )
        source = skill.path.relative_to(skill.library.path).as_posix()
        notice = (
            f"## {skill.library.name}/{source} @ {sha}\n\n"
            f"Source: {skill.library.repo}\nPath: {source}\nCommit: {sha}\n"
            f"License: {license_id}" + (f" ({license_file.name})" if license_file else "") + "\n"
        )
        if license_text:
            notice += "\n" + license_text.strip() + "\n"
        for filename in ("NOTICE", "NOTICE.txt", "NOTICE.md"):
            if (skill.library.path / filename).is_file():
                notice += "\n" + (skill.library.path / filename).read_text(encoding="utf-8").strip() + "\n"
                break
        destination = repo_path(root, f".claude/skills/{skill.path.name}")
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(skill.path, destination)
        notices = repo_path(root, "THIRD_PARTY_NOTICES.md")
        original = notices.read_text(encoding="utf-8") if notices.exists() else "# Third-party notices\n"
        if notice.splitlines()[0] not in original.splitlines():
            notices.write_text(original.rstrip() + "\n\n" + notice, encoding="utf-8")
        return skill.path.name
