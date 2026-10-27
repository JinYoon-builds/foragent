"""Sync skill libraries, inspect their metadata, and install selected skills."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any

import yaml

from foragent.config import Config, SKILL_ROLES, load_global_config, register_skill_library
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
    copies: tuple[Path, ...] = ()
    runs_scripts: bool = False
    role: str | None = None


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


def _runs_scripts(directory: Path) -> bool:
    """Report script files from their paths; never inspect or run their contents."""
    extensions = {".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".ps1", ".bat", ".cmd"}
    return any(path.is_file() and ("scripts" in path.relative_to(directory).parts[:-1]
                                  or path.suffix.lower() in extensions)
               for path in directory.rglob("*"))


def _frontmatter(path: Path) -> dict[str, Any]:
    try:
        match = re.match(r"\A---\s*\n(.*?)\n---(?:\s*\n|$)", path.read_text(encoding="utf-8"), re.S)
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillError(f"could not read skill: {exc}") from exc
    try:
        meta = yaml.safe_load(match[1]) if match else None
    except yaml.YAMLError as exc:
        raise SkillError(f"invalid skill frontmatter: {exc}") from exc
    if not isinstance(meta, dict) or any(not isinstance(meta.get(key), str) for key in ("name", "description")):
        raise SkillError("skill requires name and description frontmatter")
    return meta


class SkillManager:
    def __init__(self, config: Config | None = None, *, libraries_dir: Path | None = None,
                 run: Callable[[list[str]], str] | None = None):
        config = config or load_global_config()
        directory = libraries_dir or Path.home() / ".local/share/foragent/libraries"
        self.config = config
        self.libraries_dir = directory
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
                self._clone(library)
            synced.append(library.name)
        return synced

    def _clone(self, library: Library) -> None:
        try:
            library.path.parent.mkdir(parents=True, exist_ok=True)
            self.run(["git", "clone", "--", library.repo, str(library.path)])
        except (OSError, subprocess.SubprocessError) as exc:
            message = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
            raise SkillError(f"could not clone library {library.name}: {message or exc}") from exc

    def skills(self, *, available_only: bool = False) -> list[Skill]:
        result = []
        for library in self.libraries:
            if not library.path.is_dir():
                if available_only:
                    continue
                if library.path.exists():
                    raise SkillError(f"library {library.name} path is not a directory: {library.path}")
                self._clone(library)
            result.extend(self._library_skills(library))
        return result

    def _library_skills(self, library: Library) -> list[Skill]:
        return self._scan(library)[0]

    def _scan(self, library: Library) -> tuple[list[Skill], list[dict[str, str]]]:
        """Return the library's valid skills and a one-line error for each unreadable SKILL.md."""
        paths = {path for pattern in ("*/skills/*/SKILL.md", "skills/*/SKILL.md") for path in library.path.glob(pattern)}
        groups: dict[str, list[Path]] = {}
        # Prefer the shared copy when present; otherwise select a tool copy deterministically.
        for path in sorted(paths, key=lambda path: (len(path.relative_to(library.path).parts), path)):
            relative = path.relative_to(library.path).parts
            identity = path.parent.name if len(relative) == 3 or relative[0].startswith(".") else f"{relative[0]}/{path.parent.name}"
            groups.setdefault(identity, []).append(path.parent)
        result, skipped = [], []
        for identity, copies in sorted(groups.items()):
            path = copies[0] / "SKILL.md"
            try:
                meta = _frontmatter(path)
            except SkillError as exc:
                skipped.append({"path": path.relative_to(library.path).as_posix(), "error": " ".join(str(exc).split())})
                continue
            license_id, license_file = _license(path.parent, library.path)
            skill_id = f"{library.prefix}:{identity}"
            result.append(Skill(skill_id, meta["name"], meta["description"], library, path.parent,
                                license_id, license_file, tuple(copies), any(_runs_scripts(copy) for copy in copies),
                                self.config.skill_roles.get(skill_id)))
        return result, skipped

    @staticmethod
    def _summary(skill: Skill) -> dict[str, Any]:
        return {"id": skill.id, "name": skill.name, "description": skill.description, "license": skill.license,
                "runs_scripts": skill.runs_scripts, "copies": sorted(copy.relative_to(skill.library.path).as_posix()
                                                                      for copy in skill.copies), "role": skill.role}

    def catalog(self) -> list[dict[str, Any]]:
        return [self._summary(skill) for skill in self.skills()]

    def add(self, source: str, *, approve: bool = False, roles: list[str] | None = None) -> dict[str, Any]:
        """Preview a temporary checkout; register only explicitly approved, classified skills."""
        assignments = []
        for assignment in roles or []:
            identity, separator, role = assignment.partition("=")
            if not separator or not identity or role not in SKILL_ROLES:
                raise SkillError(f"invalid skill role: {assignment}; expected SKILL=style|review|tool|domain")
            assignments.append((identity, role))
        if assignments and not approve:
            raise SkillError("--role requires --approve")
        repo = str(Path(source).expanduser().resolve()) if Path(source).expanduser().exists() else source
        library = next((library for library in self.libraries if library.repo == repo), None)
        if library is None:
            name = re.split(r"[/:]", repo.rstrip("/"))[-1].removesuffix(".git")
            candidate = replace(self.config, skill_libraries=[*self.config.skill_libraries, {"name": name, "repo": repo}])
            library = SkillManager(candidate, libraries_dir=self.libraries_dir, run=self.run).libraries[-1]
        with TemporaryDirectory(prefix="foragent-skills-") as directory:
            preview = replace(library, path=Path(directory) / library.name)
            self._clone(preview)
            skills, skipped = self._scan(preview)
            if skipped and not skills:
                raise SkillError(f"no valid skills in library {library.name}: "
                                 + "; ".join(f"{item['path']}: {item['error']}" for item in skipped))
            if not skills:
                raise SkillError(f"no skills found in library {library.name}")
            confirmed = {}
            for identity, role in assignments:
                skill = self._select(identity, skills=skills)
                if skill.id in confirmed and confirmed[skill.id] != role:
                    raise SkillError(f"conflicting roles for {skill.id}")
                confirmed[skill.id] = role
            if approve:
                missing = [skill.id for skill in skills if skill.id not in confirmed]
                if missing:
                    raise SkillError("confirm a role for each skill with --role SKILL=style|review|tool|domain: "
                                     + ", ".join(missing))
                self.config = register_skill_library(library.name, repo, confirmed)
                if library not in self.libraries:
                    self.libraries.append(library)
            return {"source": source, "skills": [self._summary(replace(skill, role=confirmed.get(skill.id, skill.role)))
                                                 for skill in skills], "skipped": skipped, "approved": approve}

    def _select(self, identity: str, *, skills: list[Skill] | None = None) -> Skill:
        skills = self.skills() if skills is None else skills
        exact = next((skill for skill in skills if skill.id == identity), None)
        if exact is not None:
            return exact
        matches = []
        for skill in skills:
            relative = skill.id.split(":", 1)[1]
            aliases = {relative, skill.name}
            for copy in skill.copies:
                parts = copy.relative_to(skill.library.path).parts
                aliases.add(copy.name if len(parts) == 2 else f"{parts[0]}/{copy.name}")
            if any(identity in {alias, f"{skill.library.prefix}:{alias}", f"{skill.library.name}/{alias}",
                                f"{skill.library.name}:{alias}"} for alias in aliases):
                matches.append(skill)
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise SkillError(f"ambiguous skill: {identity}; use one of: {', '.join(skill.id for skill in matches)}")
        raise SkillError(f"unknown skill: {identity}")

    def _repo_skill(self, identity: str, root: Path) -> Skill | None:
        """A product skill kept in the repository's skills/<name>/, which wins by name over libraries."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", identity) or identity in {".", ".."}:
            return None
        path = repo_path(root, f"skills/{identity}")
        if not (path / "SKILL.md").is_file():
            return None
        meta = _frontmatter(path / "SKILL.md")
        library = Library("repo", str(root), root)
        license_id, license_file = _license(path, root)
        skill_id = f"repo:{identity}"
        return Skill(skill_id, meta["name"], meta["description"], library, path, license_id, license_file,
                     (path,), _runs_scripts(path), self.config.skill_roles.get(skill_id))

    def check_install(self, identity: str, root: Path, *, commit: bool = False) -> Skill:
        skill = self._repo_skill(identity, root) or self._select(identity)
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
    """Copy repository product skills, local installs or cached library skills, leaving the checkout's own intact."""
    copied = []
    for name in dict.fromkeys(names):
        if not isinstance(name, str) or not name or Path(name).name != name or name in {".", ".."}:
            continue
        relative = f".claude/skills/{name}"
        destination = repo_path(worktree, relative)
        if destination.exists():
            continue  # In particular, preserve user-authored, committed skills.
        # Product skills in the repository's skills/ win by name over local installs and libraries.
        source = repo_path(root, f"skills/{name}")
        if not (source / "SKILL.md").is_file():
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
