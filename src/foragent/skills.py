"""Sync skill libraries, inspect their metadata, and install selected skills."""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import sysconfig
from tempfile import TemporaryDirectory
from typing import Any

import yaml

from foragent.config import Config, DEFAULTS, SKILL_ROLES, load_global_config, register_skill_library
from foragent.license_texts import STANDARD_LICENSES
from foragent.paths import repo_path


class SkillError(ValueError):
    """A library or installation cannot be used."""


@dataclass(frozen=True)
class Library:
    name: str
    repo: str
    path: Path
    # A vendored library is pinned to a recorded revision and lists its skills; sync never pulls it.
    revision: str | None = None
    members: tuple[str, ...] | None = None

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


def _source_key(repo: str) -> str:
    """The same repository however its URL is spelled; a library's name says nothing about its source."""
    key = re.sub(r"^(?:ssh://)?git@github\.com[:/]", "https://github.com/", repo.strip())
    return key.rstrip("/").removesuffix(".git").casefold()


def trusted_source(repo: str) -> bool:
    """Whether a library comes from one of the repositories Foragent ships as its defaults."""
    return _source_key(repo) in {_source_key(entry["repo"]) for entry in DEFAULTS["skill_libraries"]}


def entry_directory(entry: str) -> str:
    """The installed directory name a `default_skills` entry (`library:folder/name`) points to."""
    return entry.rsplit(":", 1)[-1].rsplit("/", 1)[-1]


def _normalized_license(text: str) -> str:
    text = re.sub(
        r"^[ \t]*Copyright[ \t]+(?:\(c\)[ \t]*|©[ \t]*)?(?:\d{4}|<year>|\[yyyy\])[^\r\n]*$",
        "", text, flags=re.I | re.M,
    )
    return " ".join(text.split())


def _license(skill: Path, library: Path, *, nearest: bool = False, walk: bool = False) -> tuple[str, Path | None]:
    """Identify a skill's license from its own LICENSE, else the library's.

    With ``nearest`` (library catalog skills), every folder between the skill and the library root is
    tried in turn, and a standard text followed by unrelated appended text still counts as that license.
    ``walk`` tries those folders too but keeps exact matching (a project's own skills).
    """
    directories = [skill, library]
    if nearest or walk:
        directories = [skill, *(parent for parent in skill.parents if parent.is_relative_to(library))]
    license_file = next((directory / name for directory in directories for name in ("LICENSE", "LICENSE.txt")
                         if (directory / name).is_file()), None)
    if license_file is None:
        return "unknown", None
    try:
        text = license_file.read_text(encoding="utf-8")
    except UnicodeError:
        return "unknown", license_file
    normalized = _normalized_license(text)
    standards = {identity: _normalized_license(standard) for identity, standard in STANDARD_LICENSES.items()}
    if nearest:
        # The leading standard text decides; appended content (SPDX lines included) cannot override it.
        leading = next((identity for identity, expected in standards.items()
                        if normalized == expected or normalized.startswith(expected + " ")), None)
        if leading is not None:
            return leading, license_file
    spdx = re.findall(
        r"^[ \t]*(?:(?:#|//|/\*|\*)[ \t]*)?SPDX-License-Identifier:[ \t]*([^\r\n]*)", text, re.M,
    )
    if spdx:
        identities = {identity.strip().removesuffix("*/").strip().casefold() for identity in spdx}
        if len(identities) != 1 or not next(iter(identities)):
            return "unknown", license_file
        identity = spdx[0].strip().removesuffix("*/").strip()
        return next((known for known in OPEN_SOURCE_LICENSES if known.casefold() == identity.casefold()), identity), license_file
    return next((identity for identity, expected in standards.items() if normalized == expected), "unknown"), license_file


def _copyright_line(line: str) -> bool:
    """A copyright or attribution line: it starts with `Copyright` or `©`. What follows is not judged."""
    return re.match(r"(?:Copyright\b|©)", line.strip()) is not None


def _terms(text: str) -> str:
    """The license text apart from its copyright lines, with whitespace collapsed."""
    return " ".join(" ".join(line for line in text.splitlines() if not _copyright_line(line)).split())


# A license declaration: any line whose label (the text before its first colon) names SPDX or a license,
# in any case, spacing or word order and behind a comment mark (`SPDX License Identifier:`, `License:` …).
_DECLARATION = re.compile(r"[\W_]*([^:]*?(?:spdx|licen[cs]e)[^:]*?)[ \t]*:(?!//)[ \t]*(.*)", re.I)


def _declaration(line: str) -> tuple[str, str] | None:
    """(label, permissive license or "unknown") when ``line`` declares a license, else None.

    The value is read as an SPDX expression: every identifier joined by `AND`/`OR` (and parentheses)
    must be a permissive one. Anything else (`WITH`, `LicenseRef-…`, words, nothing) is "unknown".
    """
    if (match := _DECLARATION.fullmatch(line.strip())) is None:
        return None
    words = [word for word in re.split(r"[\s()]+", match[2].strip().removesuffix("*/")) if word]
    identities = [word for word in words if word.upper() not in {"AND", "OR"}]
    known = [next((name for name in OPEN_SOURCE_LICENSES if name.casefold() == word.casefold()), None)
             for word in identities]
    return " ".join(match[1].split()), (known[0] if known and all(known) else "unknown")


def _spdx(line: str) -> str | None:
    """The permissive license a declaration line names, "unknown" for any other declaration, None for no declaration."""
    return None if (found := _declaration(line)) is None else found[1]


_URL = re.compile(r"<?https?://\S+")
# A labelled notice: one to three words, a colon, then anything (`Source: …`, `Upstream: …`).
_LABEL = re.compile(r"[^\W\d_][\w-]*(?: [\w-]+){0,2}[ \t]*:(?!//)[ \t]*\S.*")
# An attribution: `<work> by <anything>`, the work being one word.
_BY = re.compile(r"\S+ by \S.*")


def _notice(line: str) -> bool:
    """A line allowed around a complete license text, and not judged (#1652 scope, 2026-11-02).

    Copyright lines, permissive SPDX lines, URL lines, labelled lines (`Source:`, `Attribution:`,
    `Upstream:` …) and `<work> by …` attributions pass whatever they say: their content is a NOTE for
    the person who approves the role table or a library, the real filter. Only unlabelled sentences
    are judged, by failing: they may add a condition.
    """
    line = line.strip()
    if _copyright_line(line):
        return True
    # A declaration is always judged and never falls through to the unjudged forms below.
    if (identity := _spdx(line)) is not None:
        return identity in OPEN_SOURCE_LICENSES
    return bool(_URL.fullmatch(line) or _LABEL.fullmatch(line) or _BY.fullmatch(line))


def _permissive(text: str) -> str:
    """The permissive license a whole LICENSE text is, else "unknown" (Jin's rules, 2026-11-02 and #1652).

    It passes when it holds a known permissive license word for word (copyright lines and whitespace
    aside) and every other nonblank line, before or after that text, is a `_notice`. The older short
    form also passes: one `SPDX-License-Identifier:` line naming a permissive license plus copyright
    lines and nothing else. Everything else fails; the copyright names are never judged.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    declarations = [line for line in lines if not _copyright_line(line)]
    # The older short form is one `SPDX-License-Identifier:` line only; no other declaration stands in for terms.
    if len(declarations) == 1 and (found := _declaration(declarations[0])) is not None:
        label, identity = found
        return identity if label.casefold() == "spdx-license-identifier" else "unknown"
    standards = {identity: _terms(standard) for identity, standard in STANDARD_LICENSES.items()}
    # The license text is one run of lines; only notices may stand before or after it.
    notices = [_notice(line) for line in lines]
    first = next((index for index, notice in enumerate(notices) if not notice), len(lines))
    last = next((index for index in range(len(lines) - 1, -1, -1) if not notices[index]), -1)
    for start in range(first + 1):
        for end in range(max(last + 1, start + 1), len(lines) + 1):
            terms = _terms("\n".join(lines[start:end]))
            if terms and (identity := next((name for name, value in standards.items() if terms == value), None)):
                return identity
    return "unknown"


_LICENSE_FILE = re.compile(r"(?:licen[cs]e|copying)", re.I)
# Where an installed default records its library, path, revision and license.
SOURCE_FILE = "SOURCE"


def redistributable_license(directory: Path, base: Path) -> tuple[str, Path | None]:
    """The license over ``directory``, judged for default use by `_permissive`.

    The closest folder, from ``directory`` up to ``base``, that holds any license file decides:
    every file there named LICENSE*, LICENCE* or COPYING* (LICENSE.md included) must be permissive.
    """
    for folder in (directory, *(parent for parent in directory.parents if parent.is_relative_to(base))):
        # A folder or license file that cannot be looked at hides its facts: that is "unknown", never "none".
        try:
            found = sorted(folder / name for name in os.listdir(folder) if _LICENSE_FILE.match(name))
        except OSError:
            return "unknown", folder
        if not found:
            continue
        verdicts = []
        for path in found:
            try:
                verdicts.append((_permissive(path.read_text(encoding="utf-8")), path))
            except (OSError, UnicodeError):
                verdicts.append(("unknown", path))
        return next((verdict for verdict in verdicts if verdict[0] not in OPEN_SOURCE_LICENSES), verdicts[0])
    return "unknown", None


def reached_through_symlink(path: Path) -> bool:
    """Whether any folder on ``path``'s way, ``path`` itself, or anything inside it is a symlink.

    A default must be the files its license facts describe; a link may lead anywhere. That
    includes a linked library root, project or checkout, and any folder above them.
    """
    if os.path.realpath(path) != os.path.abspath(path):
        return True
    unreadable = []  # A folder that cannot be listed could hold a link: count it as one.
    for directory, folders, files in os.walk(path, onerror=unreadable.append):
        if any(os.path.islink(os.path.join(directory, name)) for name in (*folders, *files)):
            return True
    return bool(unreadable)


def copy_license(directory: Path, base: Path, origin: Skill | None) -> str:
    """The license facts of a skill copy under ``base``; every fact that applies must allow it.

    The nearest license over the copy always applies, up to and including ``base`` (the project's
    own LICENSE); so does the library an installed ``.claude/skills`` copy came from. Another copy's
    license never stands in for this one. Returns the first restrictive fact, else an open one.
    """
    local, found = redistributable_license(directory, base)
    facts = [local] if found is not None else []
    if origin is not None and directory.parent.parent.name == ".claude":
        facts.append(redistributable_license(origin.path, origin.library.path)[0])
    facts = facts or [local]
    return next((fact for fact in facts if fact not in OPEN_SOURCE_LICENSES), facts[0])


def _skill_files(library: Path) -> list[Path]:
    """Find <...>/skills/<name>/SKILL.md at any depth, skipping hidden folders and node_modules.

    A hidden tool folder whose child is ``skills`` (``.claude/skills`` and similar) is still searched.
    Directory symlinks are followed, as the earlier glob did; a link back into its own chain is not.
    """
    found = []
    chains = {library: {os.path.realpath(library)}}
    for directory, folders, files in os.walk(library, followlinks=True):
        current = Path(directory)
        parts = current.relative_to(library).parts
        if "SKILL.md" in files and len(parts) >= 2 and parts[-2] == "skills":
            found.append(current / "SKILL.md")
        chain = chains.pop(current)
        kept = []
        for folder in sorted(folders):
            if folder == "node_modules" or (folder.startswith(".") and not (current / folder / "skills").is_dir()):
                continue
            real = os.path.realpath(current / folder)
            if real in chain:
                continue
            chains[current / folder] = chain | {real}
            kept.append(folder)
        folders[:] = kept
    return found


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


def skill_summary(path: Path) -> tuple[str, str]:
    """A SKILL.md's name and description."""
    meta = _frontmatter(path)
    return meta["name"], meta["description"]


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
            path = Path(entry["path"]).expanduser() if isinstance(entry, dict) and "path" in entry else directory / name
            vendored = isinstance(entry, dict) and entry.get("revision")
            library = Library(name, repo, path, entry["revision"] if vendored else None,
                              tuple(entry.get("skills", ())) if vendored else None)
            # Names and prefixes are public skill IDs; a pinned library's IDs never pass to another source.
            pinned = next((lib for lib in self.libraries if lib.revision is not None
                           and (lib.name == name or lib.prefix == library.prefix)), None)
            if pinned is not None:
                raise SkillError(f"library name {name} is taken by the vendored {pinned.name} library "
                                 f"({pinned.path}); rename the configured library in skill_libraries")
            if any(lib.name == name for lib in self.libraries):
                raise SkillError(f"duplicate library name: {name}")
            if any(lib.prefix == library.prefix for lib in self.libraries):
                raise SkillError(f"duplicate library prefix: {library.prefix}")
            self.libraries.append(library)

    def sync(self) -> list[str]:
        synced = []
        for library in self.libraries:
            if library.revision is not None or _is_own_source(library):
                continue  # Pinned or local libraries are their own source; pulling would move the checkout around them.
            if library.path.exists() and not (library.path / ".git").exists():
                continue  # Not a repository of its own: `git -C` would pull whatever checkout encloses it.
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
        groups: dict[tuple[str, ...], list[Path]] = {}
        # Copies of one skill share their path up to the skills folder, ignoring one hidden tool folder.
        # Prefer the shared copy when present; otherwise select a tool copy deterministically.
        paths = (_skill_files(library.path) if library.members is None
                 else [library.path / "skills" / name / "SKILL.md" for name in library.members])
        for path in sorted(paths, key=lambda path: (len(path.relative_to(library.path).parts), path)):
            base = path.relative_to(library.path).parts[:-3]
            if base and base[-1].startswith("."):
                base = base[:-1]
            groups.setdefault((*base, path.parent.name), []).append(path.parent)
        # Today's id rule: the name at the root, else <first folder>/<name>; ids that clash use the folder path.
        identities = {key: key[-1] if len(key) == 1 else f"{key[0]}/{key[-1]}" for key in groups}
        counts = Counter(identities.values())
        named = {(identities[key] if counts[identities[key]] == 1
                   else copies[0].relative_to(library.path).as_posix()): copies for key, copies in groups.items()}
        result, skipped = [], []
        for identity, copies in sorted(named.items()):
            path = copies[0] / "SKILL.md"
            try:
                meta = _frontmatter(path)
            except SkillError as exc:
                skipped.append({"path": path.relative_to(library.path).as_posix(), "error": " ".join(str(exc).split())})
                continue
            license_id, license_file = _license(path.parent, library.path, nearest=True)
            skill_id = f"{library.prefix}:{identity}"
            result.append(Skill(skill_id, meta["name"], meta["description"], library, path.parent,
                                license_id, license_file, tuple(copies), any(_runs_scripts(copy) for copy in copies),
                                self.config.skill_roles.get(skill_id)))
        return result, skipped

    @staticmethod
    def _summary(skill: Skill) -> dict[str, Any]:
        return {"id": skill.id, "name": skill.name, "description": skill.description, "license": skill.license,
                "source": skill_source(skill.path.name, skill.library, license=skill.license),
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
        # The closest LICENSE governs: a skills/LICENSE overrides the project's root one.
        license_id, license_file = _license(path, root, walk=True)
        skill_id = f"repo:{identity}"
        return Skill(skill_id, meta["name"], meta["description"], library, path, license_id, license_file,
                     (path,), _runs_scripts(path), self.config.skill_roles.get(skill_id))

    def check_install(self, identity: str, root: Path, *, commit: bool = False) -> Skill:
        skill = self._repo_skill(identity, root) or self._select(identity)
        return self._check_install(skill, root, commit=commit)

    def _check_install(self, skill: Skill, root: Path, *, commit: bool = False) -> Skill:
        """Check the selected files without selecting again (defaults resolve offline)."""
        if skill.path.is_symlink() or any(path.is_symlink() for path in skill.path.rglob("*")):
            raise SkillError(f"skill contains a symlink: {skill.id}")
        if commit:
            self._require_open_license(skill)
        destination = repo_path(root, f".claude/skills/{skill.path.name}")
        if destination.exists() and (destination.is_symlink() or not destination.is_dir()
                                     or not self._same_copy(destination, skill.path)):
            raise SkillError(f"refusing to overwrite different existing skill: {destination}")
        repo_path(root, "THIRD_PARTY_NOTICES.md" if commit else ".gitignore")
        return skill

    @staticmethod
    def _require_open_license(skill: Skill) -> None:
        if skill.license not in OPEN_SOURCE_LICENSES:
            location = str(skill.license_file) if skill.license_file else "no LICENSE or LICENSE.txt"
            raise SkillError(f"refusing to install {skill.id}: cannot confirm an open-source license "
                             f"({skill.license}; {location})")

    def check_default(self, identity: str, root: Path) -> Skill | None:
        """A default skill's license facts, checked before `fa init` changes anything.

        Returns the skill to install, or None when a permitted copy stays in .claude/skills as the
        person left it, even when the library moved on or no longer has it.
        """
        try:
            skill, failure = (self._repo_skill(identity, root)
                              or self._select(identity, skills=self.skills(available_only=True))), None
        except SkillError as exc:
            skill, failure = None, exc
        directory = root / ".claude/skills" / (skill.path.name if skill else entry_directory(identity))
        if directory.exists() or directory.is_symlink():
            if reached_through_symlink(directory):
                raise SkillError(f"refusing to keep {identity} as a default: {directory} is reached through a symlink")
            license_id = copy_license(directory, root, skill)
            if license_id not in OPEN_SOURCE_LICENSES:
                raise SkillError(f"refusing to keep {identity} as a default: {directory} has no open-source "
                                 f"license ({license_id})")
            return None
        if failure is not None:
            raise failure
        if reached_through_symlink(skill.path):
            raise SkillError(f"refusing to install {identity} as a default: {skill.path} is reached through a symlink")
        license_id, found = redistributable_license(skill.path, skill.library.path)
        if license_id not in OPEN_SOURCE_LICENSES:
            raise SkillError(f"refusing to install {identity} as a default: cannot confirm an open-source license "
                             f"({license_id}; {found or 'no LICENSE or LICENSE.txt'})")
        # This judge alone decides a default; the catalog's looser reading must neither veto nor warn.
        return replace(self._check_install(skill, root), license=license_id, license_file=found)

    def default_needs_ignore(self, skill: Skill, root: Path) -> bool:
        """Whether installing this default adds a line to .gitignore."""
        ignore = repo_path(root, ".gitignore")
        return self._ignore_rule(skill) not in (ignore.read_bytes().splitlines() if ignore.exists() else ())

    @staticmethod
    def _ignore_rule(skill: Skill) -> bytes:
        return f"/.claude/skills/{skill.path.name}/".encode()

    @staticmethod
    def default_ignore_content(original: bytes, directories: list[str]) -> bytes:
        """The installer's append-only ignore edit, also used to recognize interrupted output."""
        for directory in directories:
            rule = f"/.claude/skills/{directory}/".encode()
            if rule not in original.splitlines():
                original += (b"\n" if original and not original.endswith(b"\n") else b"") + rule + b"\n"
        return original

    @staticmethod
    def _contents(path: Path) -> dict[str, bytes]:
        return {file.relative_to(path).as_posix(): file.read_bytes() for file in path.rglob("*") if file.is_file()}

    def _same_copy(self, copy: Path, source: Path) -> bool:
        """The copy holds the source's files unchanged; only a default's kept license and SOURCE may be added."""
        theirs, ours = self._contents(source), self._contents(copy)
        added = ours.keys() - theirs.keys()
        return (all(ours.get(name) == data for name, data in theirs.items())
                and all(name == SOURCE_FILE or _LICENSE_FILE.match(name) for name in added))

    def install_default(self, identity: str, skill: Skill, root: Path) -> None:
        """Install a checked default locally, keeping its original license and its source with the copy.

        The license files that decided its eligibility are copied in when they sit above the skill
        folder (a library often licenses its skills in one root file), and SOURCE records where it came from.
        """
        self.install(identity, root, checked=skill)
        destination = repo_path(root, f".claude/skills/{skill.path.name}")
        license_id, found = redistributable_license(skill.path, skill.library.path)
        kept = []
        if found is not None:
            for path in sorted(found.parent.iterdir()):
                if path.is_file() and _LICENSE_FILE.match(path.name):
                    kept.append(path)
                    if not (destination / path.name).exists():
                        shutil.copyfile(path, destination / path.name)
        revision = skill.library.revision
        if revision is None and (skill.library.path / ".git").exists():
            try:
                revision = self.run(["git", "-C", str(skill.library.path), "rev-parse", "HEAD"]).strip()
            except (OSError, subprocess.SubprocessError):
                revision = None
        licenses = ", ".join(path.relative_to(skill.library.path).as_posix() for path in kept)
        (destination / SOURCE_FILE).write_text(
            f"Source: {skill.library.repo}\nPath: {skill.path.relative_to(skill.library.path).as_posix()}\n"
            f"Revision: {revision or 'unknown'}\nLicense: {license_id} ({licenses})\n", encoding="utf-8")

    def install(self, identity: str, root: Path, *, commit: bool = False, checked: Skill | None = None) -> str:
        """Copy a skill into .claude/skills; ``checked`` is a skill `check_default` already judged."""
        skill = checked or self.check_install(identity, root, commit=commit)
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
            updated = self.default_ignore_content(original, [skill.path.name])
            if updated != original:
                ignore.write_bytes(updated)
            return skill.path.name
        self._write_notice(skill, root)
        return skill.path.name

    def _write_notice(self, skill: Skill, root: Path) -> None:
        sha = skill.library.revision or self.run(["git", "-C", str(skill.library.path), "rev-parse", "HEAD"]).strip()
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


def _is_own_source(library: Library) -> bool:
    """A library registered by local path at that same path has no upstream of its own to pull."""
    repo = Path(library.repo).expanduser()  # Relative paths resolve against the current directory, as git does.
    return repo.exists() and repo.resolve() == library.path.expanduser().resolve()


VENDORED_MANIFEST = "skills/pstack/library.yml"


_CHECKOUT = Path(__file__).resolve().parents[2]


def vendored_library(*roots: Path | None) -> dict[str, Any] | None:
    """The library entry for skills vendored with Foragent at a pinned revision.

    The given projects' own copies come first, in order, so a moved or copied checkout finds itself;
    then Foragent's source checkout (editable installs), then the installed package data. Only the skills the manifest lists belong to it.
    """
    bases = [root for root in roots if root is not None]
    bases += [_CHECKOUT, Path(sysconfig.get_path("data")) / "share/foragent"]
    for base in bases:
        manifest = base / VENDORED_MANIFEST
        if not manifest.is_file():
            continue
        try:
            data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise SkillError(f"could not read vendored library manifest {manifest}: {exc}") from exc
        if (not isinstance(data, dict) or not all(isinstance(data.get(key), str) for key in ("name", "repo", "revision"))
                or not isinstance(data.get("skills"), dict)):
            raise SkillError(f"invalid vendored library manifest: {manifest}")
        return {"name": data["name"], "repo": data["repo"], "path": str(base), "revision": data["revision"],
                "license": data.get("license", "unknown"), "sources": data.get("sources", {}),
                "skills": sorted(data["skills"])}
    return None


def skill_source(directory: str, library: Library | None = None, *, license: str = "unknown") -> str:
    """A display attribution, shared by role defaults and the catalog; never a license judge.

    Local copies use the shipped manifest by directory name. A configured library answers
    for its own source, even when its skill has the same name as a shipped one.
    """
    if library is None or library.members is not None:
        manifest = vendored_library(library.path if library else None)
        if manifest and directory in manifest["skills"] and (library is None or library.name == manifest["name"]):
            source = manifest["sources"].get(directory, manifest)
            return f"{source['name']} ({source['license']})"
    if library is not None and _source_key(library.repo).startswith("https://github.com/anthropics/"):
        return f"Anthropic official ({license})"
    if directory.startswith("fa-") and (library is None or library.name == "repo"):
        return "Foragent-made"
    return f"{library.name if library else 'project'} ({license})"


def with_vendored_library(config: Config, *roots: Path | None) -> Config:
    """Put the vendored library before the configured ones, so a configured name cannot take its IDs."""
    entry = vendored_library(*roots)
    if entry is None or any(isinstance(item, dict) and item.get("revision") for item in config.skill_libraries):
        return config
    return replace(config, skill_libraries=[entry, *config.skill_libraries])


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
