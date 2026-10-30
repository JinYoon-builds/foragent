"""The one in-flight batch write, so an interrupted flush is undone or finished from Git facts.

Before a flush writes any file it records, for each path, the bytes before and the Git
blob id after. Whether that write was committed is read from HEAD's blobs, never guessed
from the working tree's current text, which other people and later boundaries may change.
Not committed: each file is restored to its earlier bytes. Committed: the batch's orders
and notes are marked committed. Either way the journal is then removed.

New lanes are written by ``add_lane`` and committed on their own before the batch commit,
so their files are listed separately with their earlier bytes. A lane file whose current
bytes are HEAD's was committed and stays; any other is restored, and when the lane's
configuration goes back, the skill directories this batch created go with it.
"""

import base64
import hashlib
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from foragent import order, order_references
from foragent.i18n import text
from foragent.order_files import check_write, temporary_path, write_atomic
from foragent.paths import repo_path
from foragent.state_locks import _file_lock

JOURNAL_FILE = "order-batch-journal.json"


def _directory(root: Path) -> Path:
    """The ignored local control directory; a planted symlink must not redirect it."""
    directory = order_references.real_directory(root, ".foragent")
    ignore = directory / ".gitignore"
    if ignore.is_symlink():
        raise order.PlanError(f"{ignore} must be a regular file, not a symlink")
    if not ignore.exists() or ignore.read_bytes() == b"":
        write_atomic(root, ".foragent/.gitignore", b"*\n")  # never a half-written ignore rule
    return directory


@contextmanager
def _lock(root: Path, name: str):
    path = _directory(root) / name
    if path.is_symlink():
        raise order.PlanError(f"{path} must be a regular file, not a symlink")
    with _file_lock(path):
        yield


def blob_id(data: bytes) -> str:
    """Git's blob id for these bytes, as HEAD reports it."""
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def _data(content: str | bytes) -> bytes:
    return content.encode("utf-8") if isinstance(content, str) else content


def _bytes(root: Path, name: str) -> bytes | None:
    path = repo_path(root, name)
    return path.read_bytes() if path.is_file() else None


def _encoded(data: bytes | None) -> str | None:
    return None if data is None else base64.b64encode(data).decode("ascii")


def _decoded(value: str | None) -> bytes | None:
    return None if value is None else base64.b64decode(value)


def lane_writes(root: Path, lanes: list[dict]) -> dict:
    """Every path ``add_lane`` may write for these new lanes, with its bytes now."""
    files, directories, seen = set(), {}, set()
    for lane in lanes:
        files |= {".foragent.yml", f".claude/agents/fa-{lane['name']}.md"}
        directories[lane["name"]] = []
        for identity in lane["skills"]:
            files.add(".gitignore")
            directory = f".claude/skills/{identity.split(':', 1)[-1].rsplit('/', 1)[-1]}"
            if not repo_path(root, directory).exists() and directory not in seen:
                seen.add(directory)
                directories[lane["name"]].append(directory)
    return {"files": {name: _encoded(_bytes(root, name)) for name in sorted(files)},
            "directories": directories}


def _save(directory: Path, value: dict) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, prefix=".order-batch-journal.",
                                     suffix=".tmp", delete=False) as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(stream.name, directory / JOURNAL_FILE)


def lane_done(directory: Path, root: Path, name: str) -> None:
    """A lane's own commit landed: later recovery undoes only what came after it."""
    value = json.loads((directory / JOURNAL_FILE).read_text(encoding="utf-8"))
    lanes = value["lanes"]
    lanes["files"] = {path: _encoded(_bytes(root, path)) for path in lanes["files"]}
    lanes["directories"].pop(name, None)
    _save(directory, value)


def write(directory: Path, root: Path, files: dict[str, str | bytes], *, branch: str,
          orders: dict[str, list[str]], notes: list[str], lanes: dict | None = None) -> None:
    """Record the planned write of ``files`` (and new-lane paths) before any of them changes."""
    entries = {}
    for name, content in sorted(files.items()):
        path = repo_path(root, name)
        before = path.read_bytes() if path.is_file() else None
        entries[name] = {"before": None if before is None else base64.b64encode(before).decode("ascii"),
                         "after": blob_id(_data(content))}
    _save(directory, {"branch": branch, "orders": orders, "notes": notes, "files": entries,
                      "lanes": lanes or {"files": {}, "directories": {}}})


def clear(directory: Path) -> None:
    (directory / JOURNAL_FILE).unlink(missing_ok=True)


def _restore(root: Path, name: str, before: bytes | None) -> None:
    check_write(root, name, PurePosixPath(name).parent.as_posix())  # never through a planted link
    target = repo_path(root, name)
    if before is None:
        target.unlink()
    else:
        write_atomic(root, name, before)


def _recover_lanes(root: Path, git, lanes: dict) -> None:
    files = lanes["files"]
    if not files:
        return
    heads = git.blob_shas(git.head_sha(), list(files))
    config_before, config_now = _decoded(files[".foragent.yml"]), _bytes(root, ".foragent.yml")
    # The lane exists only once its own commit holds the saved configuration; skill copies
    # and .gitignore lines start before that save, so nothing else marks it as finished.
    lane_committed = (config_now is not None and config_now != config_before
                      and heads.get(".foragent.yml") == blob_id(config_now))
    for name, encoded in files.items():
        before, current = _decoded(encoded), _bytes(root, name)
        if current == before or (lane_committed and current is not None and heads.get(name) == blob_id(current)):
            continue  # untouched, or the lane commit holds exactly these bytes
        _restore(root, name, before)
    if not lane_committed:
        # Copied skills are ignored by Git, so a partial copy is removed, never trusted.
        for relative in (path for paths in lanes["directories"].values() for path in paths):
            path = repo_path(root, relative)
            if path.is_symlink():
                raise order.PlanError(f"{relative} must be a real directory, not a symlink")
            if path.is_dir():
                shutil.rmtree(path)


def recover(directory: Path, root: Path, git, mark_committed, language: str) -> None:
    """Finish or undo a recorded write; ``mark_committed(orders, notes, head_blobs)`` records success."""
    path = directory / JOURNAL_FILE
    if path.is_symlink():
        raise order.PlanError(f"{path} must be a regular file, not a symlink")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except ValueError as exc:
        raise order.PlanError(f"{path} is not valid JSON") from exc
    # Recovery needs no configuration: the branch it wrote on is recorded here.
    if git.current_branch() != value["branch"]:
        raise order.PlanError(text("batch_recover_branch", language, branch=value["branch"]))
    lanes = value.get("lanes", {"files": {}, "directories": {}})
    files = value["files"]
    for name in [*files, *lanes["files"]]:
        leftover = temporary_path(root, name)
        if leftover.is_symlink() or leftover.is_file():
            leftover.unlink()  # an interrupted atomic write never reached its file
    _recover_lanes(root, git, lanes)
    heads = git.blob_shas(git.head_sha(), list(files)) if files else {}
    if all(heads.get(name) == entry["after"] for name, entry in files.items()):
        mark_committed(value["orders"], value["notes"], heads)
        clear(directory)
        return
    # Not committed: put back each file this batch wrote, and only those.
    for name, entry in files.items():
        target = repo_path(root, name)
        before = None if entry["before"] is None else base64.b64decode(entry["before"])
        current = target.read_bytes() if target.is_file() else None
        if current == before:
            continue
        if current is None or blob_id(current) != entry["after"]:
            raise order.PlanError(text("batch_recover_changed", language, path=name))
        _restore(root, name, before)
    clear(directory)
