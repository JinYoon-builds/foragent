"""Find test-harness changes a PR introduces outside its issue's ## Files."""

from __future__ import annotations

from collections.abc import Callable
import math
import os
from pathlib import Path, PurePosixPath
import subprocess
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


def _git(git: GitRepo, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    """Run Git without decoding, so paths and file bytes are never replaced."""
    try:
        return subprocess.run(["git", *args], cwd=git.root, capture_output=True, timeout=120)
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


def _blob(git: GitRepo, spec: bytes) -> bytes | None:
    """Exact blob bytes, or None when the path is absent on that side."""
    if _git(git, ["cat-file", "-e", os.fsdecode(spec)]).returncode != 0:
        return None
    result = _git(git, ["cat-file", "blob", os.fsdecode(spec)])
    if result.returncode != 0:
        raise GitError(result.stderr.decode("utf-8", "replace") or f"cannot read {spec!r}")
    return result.stdout


def changed_harness_paths(git: GitRepo, target_ref: str, listed: list[str]) -> list[str]:
    """Harness paths the staged PR contents change since its merge base with the target.

    Renames count as a deletion plus an addition, so both paths are reported.
    """
    base = git._own_base(target_ref)
    diff = _git(git, ["diff", "--cached", "--name-only", "--no-renames", "-z", base])
    if diff.returncode != 0:
        raise GitError(diff.stderr.decode("utf-8", "replace") or "git diff failed")
    found = []
    for raw in filter(None, diff.stdout.split(b"\0")):
        name = PurePosixPath(os.fsdecode(raw)).name
        if name == "pyproject.toml":
            before = _pytest_tables(_blob(git, base.encode() + b":" + raw))
            after = _pytest_tables(_blob(git, b":" + raw))
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
