"""Find test-harness changes a PR introduces outside its issue's ## Files."""

from __future__ import annotations

from collections.abc import Callable
from fnmatch import fnmatchcase
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


_END = object()


def _canonical(value: object) -> tuple:
    """TOML values compared by type and exact value, so 0 differs from false and nan equals nan.

    The result is a flat token sequence built with an explicit stack, so neither
    building nor comparing it recurses, however deeply the document nests.
    TOML values are never tuples, so a tuple on the stack marks a table key.
    """
    tokens: list[tuple] = []
    stack: list[object] = [value]
    while stack:
        item = stack.pop()
        if item is _END:
            tokens.append(("end",))
        elif isinstance(item, tuple):
            tokens.append(item)  # ("key", name)
        elif isinstance(item, dict):
            tokens.append(("table", len(item)))
            stack.append(_END)
            for key in sorted(item, reverse=True):
                stack.append(item[key])
                stack.append(("key", key))
        elif isinstance(item, list):
            tokens.append(("array", len(item)))
            stack.append(_END)
            stack.extend(reversed(item))
        elif isinstance(item, float):
            tokens.append(("float", "nan" if math.isnan(item) else item.hex()))
        elif hasattr(item, "isoformat"):
            tokens.append((type(item).__name__, item.isoformat()))
        else:
            tokens.append((type(item).__name__, item))
    return tuple(tokens)


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
    except Exception:  # undecodable, invalid, or too deeply nested to parse (RecursionError)
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
    return any(_glob_matches(parts, _parts(entry.encode("utf-8"))) for entry in files)


def _glob_matches(parts: tuple[bytes, ...], globs: tuple[bytes, ...]) -> bool:
    # Iterative: the set of path positions reachable after each glob component,
    # so a path with thousands of components cannot exhaust the recursion limit.
    positions = {0}
    for glob in globs:
        if glob == b"**":
            positions = set(range(min(positions), len(parts) + 1))
        else:
            positions = {i + 1 for i in positions if i < len(parts) and fnmatchcase(parts[i], glob)}
        if not positions:
            return False
    return len(parts) in positions


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


# Directories the walk and the tree listings leave out, by exact name bytes at
# any depth: Git's own data, runner scratch, virtualenvs and tool caches.
SKIP_DIRS = frozenset({b".git", b".venv", b"venv", b".uv-cache", b".tox", b"node_modules", b".foragent",
                       b"__pycache__"})
_TRACKED_NAMES = _HARNESS_NAMES_RAW | {b"pyproject.toml"}


def _harness_prefix(raw: bytes) -> bytes | None:
    """The harness-named entry a listed path is, or lies under, unless a skipped directory comes first.

    Git lists files, not directories, so pkg/conftest.py/inside.txt stands for
    a directory named pkg/conftest.py: that entry is the candidate.
    """
    parts = raw.split(b"/")
    for i, part in enumerate(parts):
        if part in SKIP_DIRS:
            return None
        if part in _TRACKED_NAMES:
            return b"/".join(parts[:i + 1])
    return None


def _walk(root: Path) -> tuple[set[bytes], set[bytes]]:
    """Every harness-named entry under root, and the places the walk could not read.

    The walk enters ignored and untracked directories alike, never follows a
    symlinked directory, and does not descend into a harness-named entry:
    whatever its type, that entry is itself a candidate.
    """
    found: set[bytes] = set()
    unreadable: set[bytes] = set()
    top = os.fsencode(root)
    stack = [b""]
    while stack:
        rel = stack.pop()
        try:
            with os.scandir(os.path.join(top, rel) if rel else top) as entries:
                for entry in entries:
                    name = entry.name
                    path = rel + b"/" + name if rel else name
                    if name in SKIP_DIRS:
                        continue
                    if name in _TRACKED_NAMES:
                        found.add(path)
                        continue
                    try:
                        # lstat, unlike is_dir(), raises for an entry that vanished.
                        is_dir = stat.S_ISDIR(entry.stat(follow_symlinks=False).st_mode)
                    except OSError:
                        unreadable.add(path)
                        continue
                    if is_dir:
                        stack.append(path)
        except OSError:
            unreadable.add(rel or b".")  # an unreadable or vanished directory or entry
    return found, unreadable


def _listed_paths(git: GitRepo, args: list[str]) -> set[bytes]:
    output = _required(_git(git, args), " ".join(args[:2]))
    return {prefix for path in output.split(b"\0") if path and (prefix := _harness_prefix(path)) is not None}


def _index_entries(git: GitRepo, raw: bytes) -> list[object]:
    """What the index records at a path, read without changing it.

    Stage 0 is what a commit publishes; it can differ from the worktree (a
    clean filter, skip-worktree or assume-unchanged, a symlink checked out as a
    regular file under core.symlinks false), so its mode and raw blob are
    judged too. During a conflict the worktree holds the resolution, so the
    conflict stages count only for their type.
    """
    listing = _required(_git(git, ["--literal-pathspecs", "ls-files", "-s", "-z", "--", os.fsdecode(raw)]),
                        "ls-files")
    sides: list[object] = []
    for entry in filter(None, listing.split(b"\0")):
        info, path = entry.split(b"\t", 1)
        if path != raw:
            sides.append(_NOT_A_FILE)  # entries below the path: Git records a directory there
            continue
        mode, sha, stage = info.split(b" ")
        if mode not in _REGULAR_MODES and mode != b"120000":
            sides.append(_NOT_A_FILE)  # submodule
        elif stage == b"0" or mode == b"120000":
            blob = _git(git, ["cat-file", "blob", sha.decode()])
            sides.append((mode, blob.stdout) if blob.returncode == 0 else _NOT_A_FILE)
    return sides


def _merge_base(git: GitRepo, target_ref: str, *heads: str) -> str:
    result = _git(git, ["merge-base", target_ref, *heads])
    if result.returncode == 1:
        return target_ref  # unrelated histories have no merge base
    return _required(result, "merge-base").strip().decode()


def changed_harness_paths(git: GitRepo, target_ref: str, files: list[str]) -> list[str]:
    """Harness paths that hold the PR, for display.

    Candidates do not come from asking Git what changed. They are every
    harness-named entry the file-system walk finds (ignored and untracked
    directories included) plus every harness-named path in the base tree,
    the HEAD tree and the index. Each is judged by raw bytes and Git mode:
    the worktree entry and the index's stage 0 against the merge base of HEAD
    (and a pending MERGE_HEAD) with the target, and the committed HEAD blob
    against HEAD's own merge base with the target. Blobs come from git
    cat-file, which applies no clean or smudge filter. A harness path that is
    not valid UTF-8, or that cannot be compared exactly, holds even when
    ## Files lists it; so does a directory the walk could not read.
    """
    found_on_disk, unreadable = _walk(git.root)
    if not callable(getattr(git, "run_bytes", None)):
        # A repository object that cannot run Git (a fixture): its changed
        # paths and the walk are the candidates; judging them needs Git, so
        # any candidate holds.
        listed_changes = {_harness_prefix(path.encode("utf-8")) for path in git.changed_paths(target_ref)}
        return [display_path(raw) for raw in sorted((listed_changes - {None}) | found_on_disk | unreadable)]
    base = git._own_base(target_ref)
    head_base = _merge_base(git, target_ref, "HEAD")
    candidates = (found_on_disk
                  | _listed_paths(git, ["ls-tree", "-r", "-z", "--full-tree", "--name-only", base])
                  | _listed_paths(git, ["ls-tree", "-r", "-z", "--full-tree", "--name-only", "HEAD"])
                  | _listed_paths(git, ["ls-files", "-z"]))
    file_mode = _git(git, ["config", "--bool", "core.fileMode"]).stdout.strip() != b"false"
    held = set(unreadable)
    for raw in candidates:
        try:
            if _judge(git, base, head_base, raw, files, file_mode, raw in found_on_disk):
                held.add(raw)
        except Exception:  # anything that stops an exact comparison holds (fail closed)
            held.add(raw)
    return [display_path(raw) for raw in sorted(held)]


def _judge(git: GitRepo, base: str, head_base: str, raw: bytes, files: list[str], file_mode: bool,
           walked: bool) -> bool:
    """Whether one harness candidate holds the PR; walked means the walk saw it on disk."""
    name = raw.rsplit(b"/", 1)[-1]
    before = _tree_side(git, base, raw)
    path = git.root / os.fsdecode(raw)
    afters = ([_disk_side(path)] if file_mode
              else [_disk_side(path, mode) for mode in _recorded_modes(git, raw)])
    if walked and None in afters:
        return True  # the walk saw it, then it vanished: it cannot be compared (fail closed)
    afters += _index_entries(git, raw)
    verdicts = {_verdict(name, before, after) for after in afters}
    # The committed HEAD blob, against HEAD's own merge base: a pending
    # merge's target-only changes are not in HEAD yet and are not the PR's.
    head_before = before if head_base == base else _tree_side(git, head_base, raw)
    verdicts.add(_verdict(name, head_before, _tree_side(git, "HEAD", raw)))
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        verdicts.add(_ALWAYS)  # Files entries are UTF-8; such a path cannot be listed exactly
    return _ALWAYS in verdicts or (_CHANGE in verdicts and not listed(raw, files))


def hold_details(paths: list[str]) -> str:
    return "Changed test-harness paths held for a person (## Files does not cover them, or they cannot be compared exactly):\n" + "\n".join(f"- `{path}`" for path in paths)
