"""Find test-harness changes a PR introduces outside its issue's ## Files."""

from __future__ import annotations

from collections.abc import Callable
from fnmatch import fnmatchcase
from functools import lru_cache
import math
import os
from pathlib import Path
import stat
import subprocess
import tomllib
from typing import TYPE_CHECKING

from foragent.git import GitError

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
    """Raw Git output through the repository object, never a process of our own.

    core.precomposeUnicode=false keeps macOS Git from rewriting NFD path bytes
    to NFC, so every path is the exact bytes stored in Git.
    """
    return git.run_bytes(["-c", "core.precomposeUnicode=false", *args])


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
    # --no-renames lists a rename as its deletion and addition (both paths)
    # without reading blobs, so an unreadable blob is judged, not an error.
    committed = _required(_git(git, ["diff", "--name-status", "-z", "--no-renames", base, "HEAD"]), "diff")
    staged = _required(_git(git, ["diff", "--cached", "--name-status", "-z", "--no-renames", "HEAD"]),
                       "diff --cached")
    status = _required(_git(git, ["status", "--porcelain=v2", "-z", "--untracked-files=all",
                                  "--ignored=matching", "--no-renames"]), "status")
    return _name_status(committed) | _name_status(staged) | _status_paths(status)


def _tree_side(git: GitRepo, rev: str, raw: bytes) -> object:
    """A committed entry: (Git mode, bytes) for a file or symlink, None when absent, else _NOT_A_FILE."""
    listing = _required(_git(git, ["--literal-pathspecs", "ls-tree", "-z", rev, "--", os.fsdecode(raw)]),
                        "ls-tree")
    entries = [entry for entry in listing.split(b"\0") if entry]
    if not entries:
        return None
    mode, kind, sha = entries[0].split(b"\t", 1)[0].split(b" ")
    if kind != b"blob" or (mode not in _REGULAR_MODES and mode != b"120000"):
        return _NOT_A_FILE  # tree or submodule
    blob = _git(git, ["cat-file", "blob", sha.decode()])
    if blob.returncode != 0:
        return _NOT_A_FILE  # an unreadable blob cannot be compared exactly
    return (mode, blob.stdout)


def _disk_side(path: Path, recorded_mode: bytes | None = None) -> object:
    """The entry as the checks find it and as publication stages it, in the same shape as _tree_side.

    The execute bit gives the Git mode as `git add` records it. With
    core.fileMode false Git ignores that bit and stages the mode it already
    records, which the caller passes as recorded_mode.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return _NOT_A_FILE
    if stat.S_ISLNK(info.st_mode):
        try:
            return (b"120000", os.fsencode(os.readlink(path)))
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
        mode = os.fstat(stream.fileno()).st_mode
        if not stat.S_ISREG(mode):
            return _NOT_A_FILE
        git_mode = recorded_mode or (b"100755" if mode & stat.S_IXUSR else b"100644")
        return (git_mode, stream.read())


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


_HARNESS_NAMES_RAW = frozenset(name.encode() for name in HARNESS_NAMES)
_NO_CHANGE, _CHANGE, _ALWAYS = "no-change", "change", "always"


def _verdict(name: bytes, before: object, after: object) -> str:
    """How a harness path's head entry compares with the merge base.

    _ALWAYS holds even when ## Files lists the path: anything the guard cannot
    compare exactly (fail closed). _CHANGE holds unless the path is listed.
    """
    if before is _NOT_A_FILE or after is _NOT_A_FILE:
        return _ALWAYS  # directory, submodule, FIFO or unreadable entry
    if any(side is not None and side[0] not in _REGULAR_MODES for side in (before, after)):
        return _ALWAYS  # a symlink on either side, changed or not
    if name == b"pyproject.toml":
        # Only the pytest tables count, so a mode-only change does not.
        tables = [_pytest_tables(side[1] if side is not None else None) for side in (before, after)]
        if _UNPARSABLE in tables:
            return _ALWAYS
        return _NO_CHANGE if tables[0] == tables[1] else _CHANGE
    return _NO_CHANGE if before == after else _CHANGE  # Git mode and content


def _parts(path: bytes) -> tuple[bytes, ...]:
    # Split on "/" only. Nothing is normalized: an entry with an empty, "." or
    # leading "/" component matches no repository path, so it authorizes nothing.
    return tuple(path.split(b"/"))


def listed(path: bytes, files: list[str]) -> bool:
    """Whether a raw Git path matches a ## Files entry, compared as bytes.

    Each entry is UTF-8 encoded and matched like policy._matches: * and ? stay
    within a component, ** spans components, a literal entry needs equal bytes.
    """
    parts = _parts(path)
    for entry in files:
        globs = _parts(entry.encode("utf-8"))

        @lru_cache(None)
        def match(i: int, j: int) -> bool:
            if j == len(globs):
                return i == len(parts)
            if globs[j] == b"**":
                return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
            return i < len(parts) and fnmatchcase(parts[i], globs[j]) and match(i + 1, j + 1)

        if match(0, 0):
            return True
    return False


def display_path(raw: bytes) -> str:
    """A path for a comment: valid UTF-8 as is, anything else in Git's C-quoted form."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    escapes = {7: "\\a", 8: "\\b", 9: "\\t", 10: "\\n", 11: "\\v", 12: "\\f", 13: "\\r",
               34: '\\"', 92: "\\\\"}
    return '"' + "".join(escapes.get(byte) or (chr(byte) if 32 <= byte < 127 else f"\\{byte:03o}")
                         for byte in raw) + '"'


def _recorded_modes(git: GitRepo, raw: bytes) -> list[bytes]:
    """The modes Git records for a path when core.fileMode is false (it ignores the execute bit).

    HEAD's recorded mode stands in, 100644 for a new file. A different staged
    mode (`git update-index --chmod`) is judged too, failing closed.
    """
    head = _tree_side(git, "HEAD", raw)
    modes = [head[0] if isinstance(head, tuple) and head[0] in _REGULAR_MODES else b"100644"]
    listing = _required(_git(git, ["--literal-pathspecs", "ls-files", "-s", "-z", "--", os.fsdecode(raw)]),
                        "ls-files")
    for entry in filter(None, listing.split(b"\0")):
        info, path = entry.split(b"\t", 1)
        mode, _sha, stage = info.split(b" ")
        if path == raw and stage in (b"0", b"2") and mode in _REGULAR_MODES and mode not in modes:
            modes.append(mode)  # staged, or "ours" during a conflicted merge
    return modes


def _index_entries(git: GitRepo, raw: bytes) -> list[object]:
    """Index entries at a path that the worktree cannot show, read without changing the index.

    Staging replaces a normal regular entry with the worktree file, so the
    worktree side already stands for it. Two kinds of recorded entry are not
    replaced and are judged here: a non-regular type (a symlink checked out as
    a regular file under core.symlinks false, a submodule, a directory), and
    an entry Git does not refresh from disk (skip-worktree or assume-unchanged).
    """
    listing = _required(_git(git, ["--literal-pathspecs", "ls-files", "-v", "-s", "-z", "--", os.fsdecode(raw)]),
                        "ls-files")
    sides: list[object] = []
    for entry in filter(None, listing.split(b"\0")):
        info, path = entry.split(b"\t", 1)
        if path != raw:
            sides.append(_NOT_A_FILE)  # entries below the path: Git records a directory there
            continue
        tag, mode, sha, _stage = info.split(b" ")
        if mode not in _REGULAR_MODES and mode != b"120000":
            sides.append(_NOT_A_FILE)  # submodule
        elif mode == b"120000" or tag != b"H":
            blob = _git(git, ["cat-file", "blob", sha.decode()])
            sides.append((mode, blob.stdout) if blob.returncode == 0 else _NOT_A_FILE)
    return sides


def changed_harness_paths(git: GitRepo, target_ref: str, files: list[str]) -> list[str]:
    """Harness paths whose head entry differs from the merge base with the target, for display.

    The guard runs before staging, and publication stages the worktree it
    judged, so the worktree is both what the checks load and what the PR
    carries. Candidates come from every Git view as raw path bytes, never
    decoded; each one is judged by its worktree entry (Git mode and content)
    against the merge base, so a pending merge's target-only changes are
    inherited, not introduced. A harness path that is not valid UTF-8, or
    that cannot be compared exactly, holds even when ## Files lists it.
    """
    can_run_git = callable(getattr(git, "run_bytes", None))
    if can_run_git:
        base = git._own_base(target_ref)
        candidates = measured_changes(git, base)
    else:
        # A repository object that cannot run Git (a fixture) reports only its
        # changed paths; they are judged the same way, as bytes.
        base, candidates = None, {path.encode("utf-8") for path in git.changed_paths(target_ref)}
    harness = sorted(raw for raw in candidates
                     if raw.rsplit(b"/", 1)[-1] in _HARNESS_NAMES_RAW | {b"pyproject.toml"})
    if not harness:
        return []
    base = base or git._own_base(target_ref)
    file_mode = _git(git, ["config", "--bool", "core.fileMode"]).stdout.strip() != b"false"
    found = []
    for raw in harness:
        name = raw.rsplit(b"/", 1)[-1]
        before = _tree_side(git, base, raw)
        path = git.root / os.fsdecode(raw)
        afters = ([_disk_side(path)] if file_mode
                  else [_disk_side(path, mode) for mode in _recorded_modes(git, raw)])
        # Git's recorded entries the worktree cannot show: index entries of another
        # type or not refreshed from disk, and HEAD's entry type.
        afters += _index_entries(git, raw)
        verdicts = {_verdict(name, before, after) for after in afters}
        head = _tree_side(git, "HEAD", raw)
        if head is _NOT_A_FILE or (head is not None and head[0] not in _REGULAR_MODES):
            verdicts.add(_ALWAYS)  # a symlink or submodule recorded in HEAD
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            verdicts.add(_ALWAYS)  # Files entries are UTF-8; such a path cannot be listed exactly
        if _ALWAYS in verdicts or (_CHANGE in verdicts and not listed(raw, files)):
            found.append(display_path(raw))
    return found


def hold_details(paths: list[str]) -> str:
    return "Changed test-harness paths held for a person (## Files does not cover them, or they cannot be compared exactly):\n" + "\n".join(f"- `{path}`" for path in paths)
