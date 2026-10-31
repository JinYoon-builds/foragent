"""The one in-flight batch write, so an interrupted flush is undone or finished from Git facts.

Before a flush writes anything it lists every path it will write (order files and notes)
with its earlier bytes and the base commit. Orders that add lanes never join a batch, so
nothing here creates, keeps or removes lane configuration. The batch commit is created
first and its sha saved here before HEAD moves to it (``GitRepo.commit_recorded``), so a
commit HEAD holds is always recorded. Its files are committed from memory and written to
the checkout only after the commit, so before it nothing of them is in the shared checkout.
Whether the batch committed is that one fact:

- the recorded sha is an ancestor of HEAD: the batch is done; paths it had not written
  yet take HEAD's bytes, every listed path is unstaged to HEAD, and its checkpoint (orders
  and notes marked committed with that commit) is written;
- no sha is recorded, or it is not an ancestor: the batch did not land; every listed path
  is put back and unstaged, so nothing stays in the index: to HEAD's bytes when a commit
  since the base changed it (someone else's committed work), else to its earlier bytes.

Either way the journal is then removed and a later flush retries the remaining work.
"""

import base64
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from foragent import order, order_references
from foragent.i18n import text
from foragent.order_files import check_write, temporary_path, write_atomic
from foragent.paths import repo_path
from foragent.state_locks import _file_lock

JOURNAL_FILE = "order-batch-journal.json"
VERSION = 7


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


def _save(directory: Path, value: dict) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, prefix=".order-batch-journal.",
                                     suffix=".tmp", delete=False) as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(stream.name, directory / JOURNAL_FILE)


def _load(directory: Path) -> dict:
    return json.loads((directory / JOURNAL_FILE).read_text(encoding="utf-8"))


def landed(git, sha: str | None) -> bool:
    """The one recovery predicate: a recorded commit that is an ancestor of HEAD."""
    return sha is not None and git.is_ancestor(sha, "HEAD")


def batch_done(directory: Path, sha: str) -> None:
    """Record the batch commit's sha; ``commit_recorded`` calls this before HEAD moves to it."""
    value = _load(directory)
    value["batch_commit"] = sha
    _save(directory, value)


def write(directory: Path, root: Path, files: dict[str, str | bytes], *, branch: str, base: str,
          orders: dict[str, list[str]], notes: list[str]) -> None:
    """List every path the flush will write, with its bytes now, before any of them changes."""
    _save(directory, {"version": VERSION, "branch": branch, "base": base, "orders": orders, "notes": notes,
                      "files": {name: _encoded(_bytes(root, name)) for name in sorted(files)},
                      "batch_commit": None})


def clear(directory: Path) -> None:
    (directory / JOURNAL_FILE).unlink(missing_ok=True)


def _restore(root: Path, name: str, before: bytes | None) -> None:
    if _bytes(root, name) == before and not repo_path(root, name).is_symlink():
        return
    parent = PurePosixPath(name).parent.as_posix()
    check_write(root, name, parent)  # never through a planted link
    if before is None:
        repo_path(root, name).unlink()
    else:
        order_references.real_directory(root, parent)  # one real directory at a time
        check_write(root, name, parent)
        write_atomic(root, name, before)


def _targets(git, base: str, files: dict[str, str | None]) -> dict[str, bytes | None]:
    """The bytes each path goes back to: HEAD's when a commit since ``base`` changed it, else earlier."""
    names = list(files)
    then, now = (git.blob_shas(base, names), git.blob_shas("HEAD", names)) if names else ({}, {})
    return {name: (None if now[name] == "missing" else git.blob_bytes(now[name].encode()))
            if then[name] != now[name] else _decoded(encoded) for name, encoded in files.items()}


def _put_back(root: Path, git, base: str, files: dict[str, str | None]) -> None:
    """Undo a batch that is not in HEAD's history; work someone else committed since stays."""
    for name, target in _targets(git, base, files).items():
        _restore(root, name, target)
    git.restore_staged_paths("HEAD", list(files))  # nothing of the batch stays staged


def _finish(root: Path, git, files: dict[str, str | None]) -> None:
    """Complete a landed batch that stopped before writing the checkout or syncing the index.

    Order files and notes reach the checkout only after the commit, so a path still holding
    its recorded earlier bytes was never written: it takes HEAD's bytes. Every listed path
    is then unstaged to HEAD, so no reversal of the batch stays staged.
    """
    names = list(files)
    heads = git.blob_shas("HEAD", names) if names else {}
    for name, encoded in files.items():
        if _bytes(root, name) == _decoded(encoded):
            _restore(root, name, None if heads[name] == "missing" else git.blob_bytes(heads[name].encode()))
    git.restore_staged_paths("HEAD", names)


def recover(directory: Path, root: Path, git, mark_committed, language: str) -> None:
    """Finish or undo a recorded write; ``mark_committed(orders, notes, blobs, commit)`` records success."""
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
    files = value["files"]
    for name in files:
        leftover = temporary_path(root, name)
        if leftover.is_symlink() or leftover.is_file():
            leftover.unlink()  # an interrupted atomic write never reached its file
    commit = value["batch_commit"]
    if landed(git, commit):
        _finish(root, git, files)
        # Committed blobs come from that commit, so later commits cannot change them.
        mark_committed(value["orders"], value["notes"], git.blob_shas(commit, list(files)), commit)
        clear(directory)
        return
    _put_back(root, git, value["base"], files)
    clear(directory)
