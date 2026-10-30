"""The one in-flight batch write, so an interrupted flush is undone or finished from Git facts.

Before a flush writes anything it lists every path it will write (order files, notes and
new-lane files) with its earlier bytes, the base commit and a fresh token. New lanes are
committed in the batch commit itself, whose message carries ``Foragent-Batch: <token>``;
its sha is also saved right after it. Whether the batch committed is a Git fact only:

- the recorded sha is an ancestor of HEAD, or HEAD's history has a commit with the token
  (it stopped before the sha was saved): the batch is done; only its checkpoint (orders
  and notes marked committed with that commit) is written, and no file is touched;
- otherwise every listed path is put back and unstaged, so nothing stays in the index: to
  HEAD's bytes when a commit since the base changed it (someone else's committed work),
  else to its bytes before the flush. Skill directories the flush created are removed.

Either way the journal is then removed and a later flush retries the remaining work.
"""

import base64
import json
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from foragent import order, order_references
from foragent.i18n import text
from foragent.order_files import check_write, temporary_path, write_atomic
from foragent.paths import repo_path
from foragent.state_locks import _file_lock

JOURNAL_FILE = "order-batch-journal.json"
VERSION = 4
TRAILER = "Foragent-Batch"


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
    files, directories = set(), []
    for lane in lanes:
        files |= {".foragent.yml", f".claude/agents/fa-{lane['name']}.md"}
        for identity in lane["skills"]:
            files.add(".gitignore")
            directory = f".claude/skills/{identity.split(':', 1)[-1].rsplit('/', 1)[-1]}"
            if not repo_path(root, directory).exists() and directory not in directories:
                directories.append(directory)
    return {"files": {name: _encoded(_bytes(root, name)) for name in sorted(files)}, "directories": directories}


def _save(directory: Path, value: dict) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, prefix=".order-batch-journal.",
                                     suffix=".tmp", delete=False) as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(stream.name, directory / JOURNAL_FILE)


def _load(directory: Path) -> dict:
    return json.loads((directory / JOURNAL_FILE).read_text(encoding="utf-8"))


def marker(token: str) -> str:
    """The commit-message line that identifies one batch commit in history."""
    return f"{TRAILER}: {token}"


def commit_of(git, sha: str | None, token: str | None) -> str | None:
    """The commit holding a batch, from its recorded sha or its token; None if not in HEAD's history."""
    if sha is not None and git.is_ancestor(sha, "HEAD"):
        return sha
    return git.find_commit(marker(token)) if token else None


def batch_done(directory: Path, git) -> None:
    """The batch commit exists: record its sha before anything else can be interrupted."""
    value = _load(directory)
    value["batch_commit"] = git.head_sha()
    _save(directory, value)


def write(directory: Path, root: Path, files: dict[str, str | bytes], *, branch: str, base: str,
          orders: dict[str, list[str]], notes: list[str], lanes: dict | None = None) -> None:
    """List every path the flush will write, with its bytes now, before any of them changes."""
    _save(directory, {"version": VERSION, "branch": branch, "base": base, "token": uuid.uuid4().hex,
                      "orders": orders, "notes": notes,
                      "files": {name: _encoded(_bytes(root, name)) for name in sorted(files)},
                      "batch_commit": None, "lanes": lanes or {"files": {}, "directories": []}})


def token(directory: Path) -> str:
    return _load(directory)["token"]


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


def _put_back(root: Path, git, base: str, files: dict[str, str | None]) -> None:
    """Undo a batch that is not in HEAD's history; work someone else committed since stays."""
    names = list(files)
    then, now = git.blob_shas(base, names), git.blob_shas("HEAD", names)
    for name, encoded in files.items():
        if then[name] != now[name]:
            # Someone committed this path after the batch began; keep that committed work.
            _restore(root, name, None if now[name] == "missing" else git.blob_bytes(now[name].encode()))
        else:
            _restore(root, name, _decoded(encoded))
    git.restore_staged_paths("HEAD", names)  # nothing of the batch stays staged


def _remove_directories(root: Path, directories: list[str]) -> None:
    # Copied skills are ignored by Git, so a partial copy is removed, never trusted.
    for relative in directories:
        path = repo_path(root, relative)
        if path.is_symlink():
            raise order.PlanError(f"{relative} must be a real directory, not a symlink")
        if path.is_dir():
            shutil.rmtree(path)


def recover(directory: Path, root: Path, git, mark_committed, language: str) -> None:
    """Finish or undo a recorded write; ``mark_committed(orders, notes, blobs, commit, token)`` records success."""
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
    for name in [*files, *lanes["files"]]:
        leftover = temporary_path(root, name)
        if leftover.is_symlink() or leftover.is_file():
            leftover.unlink()  # an interrupted atomic write never reached its file
    commit = commit_of(git, value["batch_commit"], value["token"])
    if commit is not None:
        # Committed blobs come from that commit, so later commits cannot change them.
        mark_committed(value["orders"], value["notes"], git.blob_shas(commit, list(files)), commit, value["token"])
        clear(directory)
        return
    _put_back(root, git, value["base"], {**lanes["files"], **files})
    _remove_directories(root, lanes["directories"])
    clear(directory)
