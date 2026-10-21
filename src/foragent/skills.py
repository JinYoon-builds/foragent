"""Sync skill libraries, inspect their metadata, and install selected skills."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess
import sys

import yaml

from foragent.config import Config, load_global_config
from foragent.license_texts import STANDARD_LICENSES
from foragent.paths import repo_path


class SkillError(ValueError):
    """A library or installation cannot be used."""


@dataclass(frozen=True)
class Library:
    name: str
    repo: str
    path: Path

    @property
    def prefix(self) -> str:
        return "kwp" if self.name == "knowledge-work-plugins" else self.name


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    description: str
    library: Library
    path: Path
    license: str
    license_file: Path | None


OPEN_SOURCE_LICENSES = {
    "Apache-2.0", "MIT", "BSD-1-Clause", "BSD-2-Clause", "BSD-3-Clause", "BSD-4-Clause", "0BSD", "ISC",
}


def _normalized_license(text: str) -> str:
    text = re.sub(
        r"^[ \t]*Copyright[ \t]+(?:\(c\)[ \t]*|©[ \t]*)?(?:\d{4}|<year>|\[yyyy\])[^\r\n]*$",
        "", text, flags=re.I | re.M,
    )
    return " ".join(text.split())


def _license(skill: Path, library: Path) -> tuple[str, Path | None]:
    license_file = next((directory / name for directory in (skill, library) for name in ("LICENSE", "LICENSE.txt")
                         if (directory / name).is_file()), None)
    if license_file is None:
        return "unknown", None
    try:
        text = license_file.read_text(encoding="utf-8")
    except UnicodeError:
        return "unknown", license_file
    spdx = re.findall(
        r"^[ \t]*(?:(?:#|//|/\*|\*)[ \t]*)?SPDX-License-Identifier:[ \t]*([^\r\n]*)", text, re.M,
    )
    if spdx:
        identities = {identity.strip().removesuffix("*/").strip().casefold() for identity in spdx}
        if len(identities) != 1 or not next(iter(identities)):
            return "unknown", license_file
        identity = spdx[0].strip().removesuffix("*/").strip()
        return next((known for known in OPEN_SOURCE_LICENSES if known.casefold() == identity.casefold()), identity), license_file
    normalized = _normalized_license(text)
    for identity, standard in STANDARD_LICENSES.items():
        if normalized == _normalized_license(standard):
            return identity, license_file
    return "unknown", license_file


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
            library = Library(name, repo, path)
            if any(lib.prefix == library.prefix for lib in self.libraries):
                raise SkillError(f"duplicate library prefix: {library.prefix}")
            self.libraries.append(library)

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

    def skills(self, *, available_only: bool = False) -> list[Skill]:
        result = []
        for library in self.libraries:
            if not library.path.is_dir():
                if available_only:
                    continue
                raise SkillError(f"library {library.name} is missing; run fa skills sync")
            paths = {path for pattern in ("*/skills/*/SKILL.md", "skills/*/SKILL.md") for path in library.path.glob(pattern)}
            for path in sorted(paths):
                match = re.match(r"\A---\s*\n(.*?)\n---(?:\s*\n|$)", path.read_text(encoding="utf-8"), re.S)
                try:
                    meta = yaml.safe_load(match[1]) if match else None
                except yaml.YAMLError as exc:
                    raise SkillError(f"invalid skill frontmatter: {path}: {exc}") from exc
                if not isinstance(meta, dict) or any(not isinstance(meta.get(key), str) for key in ("name", "description")):
                    raise SkillError(f"skill requires name and description frontmatter: {path}")
                relative = path.relative_to(library.path).parts
                identity = path.parent.name if len(relative) == 3 else f"{relative[0]}/{path.parent.name}"
                license_id, license_file = _license(path.parent, library.path)
                result.append(Skill(f"{library.prefix}:{identity}", meta["name"], meta["description"],
                                    library, path.parent, license_id, license_file))
        return result

    def catalog(self) -> list[dict[str, str]]:
        return [{"id": skill.id, "name": skill.name, "description": skill.description, "license": skill.license}
                for skill in self.skills()]

    def _select(self, identity: str) -> Skill:
        matches = []
        for skill in self.skills():
            relative = skill.id.split(":", 1)[1]
            if identity in {skill.id, relative, f"{skill.library.name}/{relative}", f"{skill.library.name}:{relative}"}:
                matches.append(skill)
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise SkillError(f"ambiguous skill: {identity}; use one of: {', '.join(skill.id for skill in matches)}")
        raise SkillError(f"unknown skill: {identity}")

    def check_install(self, identity: str, root: Path, *, commit: bool = False) -> Skill:
        skill = self._select(identity)
        if skill.path.is_symlink() or any(path.is_symlink() for path in skill.path.rglob("*")):
            raise SkillError(f"skill contains a symlink: {identity}")
        if commit and skill.license not in OPEN_SOURCE_LICENSES:
            location = str(skill.license_file) if skill.license_file else "no LICENSE or LICENSE.txt"
            raise SkillError(f"refusing to install {skill.id}: cannot confirm an open-source license "
                             f"({skill.license}; {location})")
        destination = repo_path(root, f".claude/skills/{skill.path.name}")
        if destination.exists() and (destination.is_symlink() or not destination.is_dir()
                                     or self._contents(destination) != self._contents(skill.path)):
            raise SkillError(f"refusing to overwrite different existing skill: {destination}")
        repo_path(root, "THIRD_PARTY_NOTICES.md" if commit else ".gitignore")
        return skill

    @staticmethod
    def _contents(path: Path) -> dict[str, bytes]:
        return {file.relative_to(path).as_posix(): file.read_bytes() for file in path.rglob("*") if file.is_file()}

    def install(self, identity: str, root: Path, *, commit: bool = False) -> str:
        skill = self.check_install(identity, root, commit=commit)
        if skill.license not in OPEN_SOURCE_LICENSES:
            location = str(skill.license_file) if skill.license_file else "no LICENSE or LICENSE.txt"
            print(f"Warning: {skill.id}: cannot confirm an open-source license "
                  f"({skill.license}; {location}); installing locally only", file=sys.stderr)
        destination = repo_path(root, f".claude/skills/{skill.path.name}")
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(skill.path, destination)
        if not commit:
            ignore = repo_path(root, ".gitignore")
            original = ignore.read_bytes() if ignore.exists() else b""
            rule = f"/.claude/skills/{skill.path.name}/".encode()
            if rule not in original.splitlines():
                ignore.write_bytes(original + (b"\n" if original and not original.endswith(b"\n") else b"")
                                   + rule + b"\n")
            return skill.path.name
        self._write_notice(skill, root)
        return skill.path.name

    def _write_notice(self, skill: Skill, root: Path) -> None:
        sha = self.run(["git", "-C", str(skill.library.path), "rev-parse", "HEAD"]).strip()
        license_file = skill.license_file
        license_text = license_file.read_text(encoding="utf-8") if license_file else ""
        source = skill.path.relative_to(skill.library.path).as_posix()
        notice = (
            f"## {skill.library.name}/{source} @ {sha}\n\n"
            f"Source: {skill.library.repo}\nPath: {source}\nCommit: {sha}\n"
            f"License: {skill.license}" + (f" ({license_file.name})" if license_file else "") + "\n"
        )
        if license_text:
            notice += "\n" + license_text.strip() + "\n"
        for filename in ("NOTICE", "NOTICE.txt", "NOTICE.md"):
            if (skill.library.path / filename).is_file():
                notice += "\n" + (skill.library.path / filename).read_text(encoding="utf-8").strip() + "\n"
                break
        notices = repo_path(root, "THIRD_PARTY_NOTICES.md")
        original = notices.read_text(encoding="utf-8") if notices.exists() else "# Third-party notices\n"
        if notice.splitlines()[0] not in original.splitlines():
            notices.write_text(original.rstrip() + "\n\n" + notice, encoding="utf-8")

    def cached_skill(self, name: str) -> Path | None:
        """Resolve an installed directory name using available library caches."""
        matches = [skill for skill in self.skills(available_only=True) if skill.path.name == name]
        if len(matches) > 1:
            raise SkillError(f"ambiguous skill: {name}; install one explicitly with fa skills install")
        return matches[0].path if matches else None


def copy_lane_skills(root: Path, worktree: Path, names: list[str], manager: SkillManager) -> list[str]:
    """Copy local dependencies, leaving skills already in the checkout intact."""
    copied = []
    for name in dict.fromkeys(names):
        if not isinstance(name, str) or not name or Path(name).name != name or name in {".", ".."}:
            continue
        relative = f".claude/skills/{name}"
        destination = repo_path(worktree, relative)
        if destination.exists():
            continue  # In particular, preserve user-authored, committed skills.
        source = repo_path(root, relative)
        if not (source / "SKILL.md").is_file():
            source = manager.cached_skill(name)
        if source is None:
            continue
        if source.is_symlink() or any(path.is_symlink() for path in source.rglob("*")):
            raise SkillError(f"skill contains a symlink: {name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
        copied.append(relative)
    return copied
