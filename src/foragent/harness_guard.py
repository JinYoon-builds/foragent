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
    return _required(_git(git, ["cat-file", "blob", os.fsdecode(spec)]), f"cat-file {spec!r}")


def _required(result: subprocess.CompletedProcess[bytes], what: str) -> bytes:
    if result.returncode != 0:
        raise GitError(result.stderr.decode("utf-8", "replace") or f"git {what} failed")
    return result.stdout


def _name_status(output: bytes) -> set[bytes]:
    """Every path in `--name-status -z` output; renames and copies give both paths."""
    tokens = output.split(b"\0")
    paths: set[bytes] = set()
    i = 0
    while i < len(tokens) and tokens[i]:
        count = 2 if tokens[i][:1] in (b"R", b"C") else 1
        paths.update(tokens[i + 1:i + 1 + count])
        i += 1 + count
    return paths


def _status_paths(output: bytes) -> set[bytes]:
    """Every path in `status --porcelain=v2 -z`: changed, renamed (both), unmerged, untracked, ignored."""
    tokens = output.split(b"\0")
    paths: set[bytes] = set()
    i = 0
    while i < len(tokens):
        record = tokens[i]
        i += 1
        if record[:2] in (b"? ", b"! "):
            paths.add(record[2:])
        elif record[:2] == b"1 ":
            paths.add(record.split(b" ", 8)[8])
        elif record[:2] == b"2 ":
            paths.add(record.split(b" ", 9)[9])
            paths.add(tokens[i])  # the rename's original path
            i += 1
        elif record[:2] == b"u ":
            paths.add(record.split(b" ", 10)[10])
    return paths


def measured_changes(git: GitRepo, base: str) -> set[bytes]:
    """Paths the head publishes or the checks can load, read without touching any index.

    Committed: the tree diff between the merge base and HEAD. Uncommitted: the
    staged diff against HEAD (which also shows paths the runner unstaged, such
    as .foragent/) and every worktree entry Git reports, including force-added,
    untracked and pattern-ignored files. Ignored directories count as one entry,
    so environments such as .venv/ do not expand into their contents.
    """
    committed = _required(_git(git, ["diff", "--name-status", "-z", "-M", base, "HEAD"]), "diff")
    staged = _required(_git(git, ["diff", "--cached", "--name-status", "-z", "-M", "HEAD"]), "diff --cached")
    status = _required(_git(git, ["status", "--porcelain=v2", "-z", "--untracked-files=all",
                                  "--ignored=matching", "--no-renames"]), "status")
    return _name_status(committed) | _name_status(staged) | _status_paths(status)


def _pyproject_sides(git: GitRepo, raw: bytes) -> list[bytes | None]:
    """Every head-side version of a pyproject: committed, staged and on disk."""
    path = git.root / os.fsdecode(raw)
    try:
        disk: bytes | None = path.read_bytes()
    except FileNotFoundError:
        disk = None
    return [_blob(git, b"HEAD:" + raw), _blob(git, b":" + raw), disk]


def changed_harness_paths(git: GitRepo, target_ref: str, listed: list[str]) -> list[str]:
    """Harness paths the head changes since its merge base with the target."""
    base = git._own_base(target_ref)
    found = []
    for raw in sorted(measured_changes(git, base)):
        name = PurePosixPath(os.fsdecode(raw)).name
        if name == "pyproject.toml":
            before = _pytest_tables(_blob(git, base.encode() + b":" + raw))
            sides = [_pytest_tables(data) for data in _pyproject_sides(git, raw)]
            if before is not _UNPARSABLE and all(side is not _UNPARSABLE and side == before for side in sides):
                continue
        elif name not in HARNESS_NAMES:
            continue
        path = raw.decode("utf-8", "replace")  # Shown in comments; never used to read.
        if not touched([path], listed):
            found.append(path)
    return found


def hold_details(paths: list[str]) -> str:
    return "Changed test-harness paths not listed in ## Files:\n" + "\n".join(f"- `{path}`" for path in paths)
