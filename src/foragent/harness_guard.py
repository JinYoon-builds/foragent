"""Find test-harness changes a PR introduces outside its issue's ## Files."""

from __future__ import annotations

from collections.abc import Callable
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
import tomllib
from typing import TYPE_CHECKING

from foragent.git import GitError
from foragent.policy import touched

if TYPE_CHECKING:
    from foragent.git import GitRepo

HARNESS_NAMES = frozenset({
    "conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "sitecustomize.py", "usercustomize.py",
})
# [tool.pytest] contains [tool.pytest.ini_options] and every other sub-table.
# An empty table still differs from a missing one: pytest selects the file.
PYPROJECT_TABLES = (("tool", "pytest"), ("project", "entry-points", "pytest11"))
_UNPARSABLE = object()
# Worktree -> check run before any measurement there. Every head measurement
# goes through run_checks._check, so the guard sits between all of them.
_GUARDS: dict[Path, Callable[[], None]] = {}


def guard_measurements(root: Path, guard: Callable[[], None]) -> Callable[[], None]:
    """Run guard before every check in root until the returned function is called."""
    key = Path(root).resolve()
    _GUARDS[key] = guard

    def stop() -> None:
        if _GUARDS.get(key) is guard:
            del _GUARDS[key]

    return stop


def before_measurement(root: Path) -> None:
    if guard := _GUARDS.get(Path(root).resolve()):
        guard()


def _canonical(value: object) -> object:
    """TOML values compared by type and exact value, so 0 differs from false and nan equals nan."""
    if isinstance(value, dict):
        return ("table", tuple(sorted((key, _canonical(item)) for key, item in value.items())))
    if isinstance(value, list):
        return ("array", tuple(_canonical(item) for item in value))
    if isinstance(value, float):
        return ("float", "nan" if math.isnan(value) else value.hex())
    if hasattr(value, "isoformat"):
        return (type(value).__name__, value.isoformat())
    return (type(value).__name__, value)


def _git(git: GitRepo, args: list[str], index: Path | None = None) -> subprocess.CompletedProcess[bytes]:
    """Run Git without decoding, so paths and file bytes are never replaced."""
    env = {**os.environ, "GIT_INDEX_FILE": str(index)} if index is not None else None
    try:
        return subprocess.run(["git", *args], cwd=git.root, capture_output=True, timeout=120, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(str(exc)) from exc


def _pytest_tables(data: bytes | None) -> object:
    """The pytest tables of one side; an absent file has none, like a pytest-free file."""
    try:
        document = tomllib.loads(data.decode("utf-8")) if data is not None else {}
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return _UNPARSABLE
    tables = []
    for keys in PYPROJECT_TABLES:
        value: object = document
        for key in keys:
            value = value.get(key) if isinstance(value, dict) else None
        tables.append(None if value is None else _canonical(value))
    return tables


def _blob(git: GitRepo, spec: bytes, index: Path | None = None) -> bytes | None:
    """Exact blob bytes, or None when the path is absent on that side."""
    if _git(git, ["cat-file", "-e", os.fsdecode(spec)], index).returncode != 0:
        return None
    result = _git(git, ["cat-file", "blob", os.fsdecode(spec)], index)
    if result.returncode != 0:
        raise GitError(result.stderr.decode("utf-8", "replace") or f"cannot read {spec!r}")
    return result.stdout


def _required(result: subprocess.CompletedProcess[bytes], what: str) -> bytes:
    if result.returncode != 0:
        raise GitError(result.stderr.decode("utf-8", "replace") or f"git {what} failed")
    return result.stdout


def changed_harness_paths(git: GitRepo, target_ref: str, listed: list[str]) -> list[str]:
    """Harness paths the measured contents change since the merge base with the target.

    The measured contents are HEAD plus the worktree, read into a private index.
    The runner's own index omits paths it never publishes (.foragent/, local
    skills, its review-scope file), yet checks still load them from disk.
    Ignored untracked files stay out, as in Git. Renames count as a deletion
    plus an addition, so both paths are reported.
    """
    base = git._own_base(target_ref)
    with tempfile.TemporaryDirectory(prefix="harness-index-") as directory:
        index = Path(directory) / "index"
        # Start from a copy of the real index so unchanged files keep their stat
        # data; a single-tree merge then resets its entries to HEAD.
        current = Path(os.fsdecode(_required(_git(git, ["rev-parse", "--git-path", "index"]), "rev-parse").strip()))
        if not current.is_absolute():
            current = git.root / current
        if current.is_file():
            shutil.copyfile(current, index)
        if _git(git, ["read-tree", "-m", "HEAD"], index).returncode != 0:
            index.unlink(missing_ok=True)  # e.g. unmerged entries during a merge
            _required(_git(git, ["read-tree", "HEAD"], index), "read-tree")
        _required(_git(git, ["add", "-A", "--", "."], index), "add")
        return _harness_paths(git, base, listed, index)


def _harness_paths(git: GitRepo, base: str, listed: list[str], index: Path) -> list[str]:
    diff = _required(_git(git, ["diff", "--cached", "--name-only", "--no-renames", "-z", base], index), "diff")
    found = []
    for raw in filter(None, diff.split(b"\0")):
        name = PurePosixPath(os.fsdecode(raw)).name
        if name == "pyproject.toml":
            before = _pytest_tables(_blob(git, base.encode() + b":" + raw))
            after = _pytest_tables(_blob(git, b":" + raw, index))
            if before is not _UNPARSABLE and after is not _UNPARSABLE and before == after:
                continue
        elif name not in HARNESS_NAMES:
            continue
        path = raw.decode("utf-8", "replace")  # Shown in comments; never used to read.
        if not touched([path], listed):
            found.append(path)
    return found


def hold_details(paths: list[str]) -> str:
    return "Changed test-harness paths not listed in ## Files:\n" + "\n".join(f"- `{path}`" for path in paths)
