"""The one in-flight batch write, so an interrupted flush is undone or finished from Git facts.

Before a flush writes anything it lists every path it will write (order files, notes and
new-lane files) with its earlier bytes. Right after each new lane's own commit and right
after the batch commit, the journal records that commit's sha. Recovery decides only by
those recorded facts, never by comparing HEAD or the working tree with what was written:

- the recorded batch commit is an ancestor of HEAD: the batch is done; only its checkpoint
  (orders and notes marked committed) is written;
- otherwise every listed path goes back to the bytes of the last recorded commit that is
  still an ancestor of HEAD (or to its bytes before the flush), and is unstaged, so nothing
  stays in the index. Skill directories of lanes without such a commit are removed.

Either way the journal is then removed and a later flush retries the remaining work.
"""

import base64
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
VERSION = 2


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
    start = {"lane": None, "commit": None, "files": {name: _encoded(_bytes(root, name)) for name in files}}
    return {"paths": sorted(files), "directories": directories, "checkpoints": [start]}


def _save(directory: Path, value: dict) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, prefix=".order-batch-journal.",
                                     suffix=".tmp", delete=False) as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(stream.name, directory / JOURNAL_FILE)


def _load(directory: Path) -> dict:
    return json.loads((directory / JOURNAL_FILE).read_text(encoding="utf-8"))


def lane_done(directory: Path, root: Path, name: str, git) -> None:
    """A lane's own commit landed: record its sha and the lane files' bytes at that commit."""
    value = _load(directory)
    lanes = value["lanes"]
    lanes["checkpoints"].append({"lane": name, "commit": git.head_sha(),
                                 "files": {path: _encoded(_bytes(root, path)) for path in lanes["paths"]}})
    _save(directory, value)


def batch_done(directory: Path, git) -> None:
    """The batch commit exists: record its sha before anything else can be interrupted."""
    value = _load(directory)
    value["batch_commit"] = git.head_sha()
    _save(directory, value)


def write(directory: Path, root: Path, files: dict[str, str | bytes], *, branch: str,
          orders: dict[str, list[str]], notes: list[str], lanes: dict | None = None) -> None:
    """List every path the flush will write, with its bytes now, before any of them changes."""
    _save(directory, {"version": VERSION, "branch": branch, "orders": orders, "notes": notes,
                      "files": {name: _encoded(_bytes(root, name)) for name in sorted(files)},
                      "batch_commit": None,
                      "lanes": lanes or {"paths": [], "directories": {}, "checkpoints": [
                          {"lane": None, "commit": None, "files": {}}]}})


def clear(directory: Path) -> None:
    (directory / JOURNAL_FILE).unlink(missing_ok=True)


def _restore(root: Path, name: str, before: bytes | None) -> None:
    if _bytes(root, name) == before and not repo_path(root, name).is_symlink():
        return
    check_write(root, name, PurePosixPath(name).parent.as_posix())  # never through a planted link
    if before is None:
        repo_path(root, name).unlink()
    else:
        write_atomic(root, name, before)


def _committed(git, sha: str | None) -> bool:
    return sha is not None and git.is_ancestor(sha, "HEAD")


def _undo_lanes(root: Path, git, lanes: dict) -> None:
    """Back to the last lane commit still in HEAD's history; later lanes' writes go."""
    kept = lanes["checkpoints"][0]
    done = set()
    for checkpoint in lanes["checkpoints"][1:]:
        if not _committed(git, checkpoint["commit"]):
            break
        kept = checkpoint
        done.add(checkpoint["lane"])
    for name in lanes["paths"]:
        _restore(root, name, _decoded(kept["files"][name]))
    # Copied skills are ignored by Git, so a partial copy is removed, never trusted.
    for lane, paths in lanes["directories"].items():
        if lane in done:
            continue
        for relative in paths:
            path = repo_path(root, relative)
            if path.is_symlink():
                raise order.PlanError(f"{relative} must be a real directory, not a symlink")
            if path.is_dir():
                shutil.rmtree(path)
    git.restore_staged_paths("HEAD", lanes["paths"])


def recover(directory: Path, root: Path, git, mark_committed, language: str) -> None:
    """Finish or undo a recorded write; ``mark_committed(orders, notes, blobs)`` records success."""
    path = directory / JOURNAL_FILE
    if path.is_symlink():
        raise order.PlanError(f"{path} must be a regular file, not a symlink")
    try:
        value = _load(directory)
    except FileNotFoundError:
        return
    except ValueError as exc:
        raise order.PlanError(f"{path} is not valid JSON") from exc
    if value.get("version") != VERSION:
        raise order.PlanError(text("batch_recover_format", language, path=path))
    # Recovery needs no configuration: the branch it wrote on is recorded here.
    if git.current_branch() != value["branch"]:
        raise order.PlanError(text("batch_recover_branch", language, branch=value["branch"]))
    files, lanes = value["files"], value["lanes"]
    for name in [*files, *lanes["paths"]]:
        leftover = temporary_path(root, name)
        if leftover.is_symlink() or leftover.is_file():
            leftover.unlink()  # an interrupted atomic write never reached its file
    if _committed(git, value["batch_commit"]):
        # Committed blobs come from the recorded commit, so later commits cannot change them.
        mark_committed(value["orders"], value["notes"], git.blob_shas(value["batch_commit"], list(files)))
        clear(directory)
        return
    _undo_lanes(root, git, lanes)
    for name, encoded in files.items():
        _restore(root, name, _decoded(encoded))
    git.restore_staged_paths("HEAD", list(files))  # nothing of the batch stays staged
    clear(directory)
