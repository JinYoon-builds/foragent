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
# An entry that is neither a regular file nor a symlink (directory, submodule,
# FIFO, unreadable): pytest cannot read it as before, so it is a change.
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
    """A committed entry: ("file", bytes), ("symlink", target), None when absent, else _NOT_A_FILE."""
    listing = _required(_git(git, ["--literal-pathspecs", "ls-tree", "-z", rev, "--", os.fsdecode(raw)]),
                        "ls-tree")
    entries = [entry for entry in listing.split(b"\0") if entry]
    if not entries:
        return None
    mode, kind, sha = entries[0].split(b"\t", 1)[0].split(b" ")
    if kind != b"blob" or (mode not in _REGULAR_MODES and mode != b"120000"):
        return _NOT_A_FILE  # tree or submodule
    content = _required(_git(git, ["cat-file", "blob", sha.decode()]), "cat-file")
    return ("symlink" if mode == b"120000" else "file", content)


def _disk_side(path: Path) -> object:
    """The entry as the checks find it and as publication stages it, in the same shape as _tree_side."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return _NOT_A_FILE
    if stat.S_ISLNK(info.st_mode):
        try:
            return ("symlink", os.fsencode(os.readlink(path)))
        except OSError:
            return _NOT_A_FILE
    if not stat.S_ISREG(info.st_mode):
        return _NOT_A_FILE  # directory, FIFO, socket or device
    try:
        # O_NONBLOCK and O_NOFOLLOW: a path swapped for a FIFO or link after
        # lstat must not stall the guard or be read through.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return _NOT_A_FILE
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return _NOT_A_FILE
        return ("file", stream.read())


def read_regular(path: Path) -> bytes | None:
    """Bytes of a regular file, following symlinks as pytest does; None for anything else.

    The runner reads worktree files the measured process can write. A FIFO in
    their place must not stall it, so the open never blocks.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None  # a directory, FIFO or device
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            return stream.read()
    except OSError:
        return None
    finally:
        if fd >= 0:
            os.close(fd)


def _changed(name: str, before: object, after: object) -> bool:
    """Whether the head side differs from the merge base, as the checks would see it."""
    if before is _NOT_A_FILE or after is _NOT_A_FILE:
        return True
    if before == after:
        return False
    if name != "pyproject.toml":
        return True
    if (before is not None and before[0] != "file") or (after is not None and after[0] != "file"):
        return True  # a symlink is not the file pytest's tables are judged in
    tables = [_pytest_tables(side[1] if side is not None else None) for side in (before, after)]
    return _UNPARSABLE in tables or tables[0] != tables[1]


def changed_harness_paths(git: GitRepo, target_ref: str, listed: list[str]) -> list[str]:
    """Harness paths whose head content differs from the merge base with the target.

    The guard runs before staging, and publication stages the worktree it
    judged, so the worktree is both what the checks load and what the PR
    carries. Candidates come from every Git view (measured_changes); each one
    is judged by its worktree entry against the merge base. Comparing content
    rather than HEAD means a pending merge's target-only changes, which HEAD
    does not have yet, are inherited, not introduced.
    """
    base = git._own_base(target_ref)
    found = []
    for raw in sorted(measured_changes(git, base)):
        name = PurePosixPath(os.fsdecode(raw)).name
        if name != "pyproject.toml" and name not in HARNESS_NAMES:
            continue
        if not _changed(name, _tree_side(git, base, raw), _disk_side(git.root / os.fsdecode(raw))):
            continue
        path = raw.decode("utf-8", "replace")  # Shown in comments; never used to read.
        if not touched([path], listed):
            found.append(path)
    return found


def hold_details(paths: list[str]) -> str:
    return "Changed test-harness paths not listed in ## Files:\n" + "\n".join(f"- `{path}`" for path in paths)
