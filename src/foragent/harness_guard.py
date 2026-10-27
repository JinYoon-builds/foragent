"""Find test-harness changes a PR introduces outside its issue's ## Files."""

from __future__ import annotations

from collections.abc import Callable
import math
import os
from pathlib import Path, PurePosixPath
import stat
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
# A side that is not a regular file (directory, symlink, submodule, FIFO,
# conflict, unreadable): pytest cannot read it as before, so it is a change.
_NOT_A_FILE = object()
_REGULAR_MODES = (b"100644", b"100755")
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


def _pytest_tables(data: object) -> object:
    """The pytest tables of one side; an absent file has none, like a pytest-free file."""
    if data is _NOT_A_FILE:
        return _UNPARSABLE
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


def _tree_side(git: GitRepo, rev: str, raw: bytes) -> object:
    """A committed side: regular blob bytes, None when absent, else _NOT_A_FILE."""
    listing = _required(_git(git, ["--literal-pathspecs", "ls-tree", "-z", rev, "--", os.fsdecode(raw)]),
                        "ls-tree")
    entries = [entry for entry in listing.split(b"\0") if entry]
    if not entries:
        return None
    mode, kind, sha = entries[0].split(b"\t", 1)[0].split(b" ")
    if mode not in _REGULAR_MODES or kind != b"blob":
        return _NOT_A_FILE  # tree, symlink or submodule
    return _required(_git(git, ["cat-file", "blob", sha.decode()]), "cat-file")


def _index_side(git: GitRepo, raw: bytes) -> object:
    """The staged side, read from the index without changing it."""
    listing = _required(_git(git, ["--literal-pathspecs", "ls-files", "-s", "-z", "--", os.fsdecode(raw)]),
                        "ls-files")
    entries = [entry.split(b"\t", 1) for entry in listing.split(b"\0") if entry]
    exact = [info.split(b" ") for info, path in entries if path == raw]
    if not exact:
        return _NOT_A_FILE if entries else None  # entries below it: a directory
    if len(exact) != 1 or exact[0][2] != b"0" or exact[0][0] not in _REGULAR_MODES:
        return _NOT_A_FILE  # conflict, symlink or submodule
    return _required(_git(git, ["cat-file", "blob", exact[0][1].decode()]), "cat-file")


def _disk_side(path: Path) -> object:
    """What pytest would read from disk; symlinks are followed as pytest follows them."""
    try:
        # O_NONBLOCK: a FIFO named pyproject.toml must not stall the guard.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return None  # absent, or a dangling symlink
    except OSError:
        return _NOT_A_FILE  # a directory, or unreadable
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return _NOT_A_FILE
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            return stream.read()
    except OSError:
        return _NOT_A_FILE
    finally:
        if fd >= 0:
            os.close(fd)


def _pyproject_sides(git: GitRepo, raw: bytes) -> list[object]:
    """Every head-side version of a pyproject: committed, staged and on disk."""
    return [_tree_side(git, "HEAD", raw), _index_side(git, raw), _disk_side(git.root / os.fsdecode(raw))]


def changed_harness_paths(git: GitRepo, target_ref: str, listed: list[str]) -> list[str]:
    """Harness paths the head changes since its merge base with the target."""
    base = git._own_base(target_ref)
    found = []
    for raw in sorted(measured_changes(git, base)):
        name = PurePosixPath(os.fsdecode(raw)).name
        if name == "pyproject.toml":
            before = _pytest_tables(_tree_side(git, base, raw))
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
