"""The one in-flight batch write, so an interrupted flush is undone or finished from Git facts.

Before a flush writes any file it records, for each path, the bytes before and the Git
blob id after. Whether that write was committed is read from HEAD's blobs, never guessed
from the working tree's current text, which other people and later boundaries may change.
Not committed: each file is restored to its earlier bytes. Committed: the batch's orders
and notes are marked committed. Either way the journal is then removed.
"""

import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path, PurePosixPath

from foragent import order
from foragent.i18n import text
from foragent.order_files import check_write
from foragent.paths import repo_path

JOURNAL_FILE = "order-batch-journal.json"


def blob_id(data: bytes) -> str:
    """Git's blob id for these bytes, as HEAD reports it."""
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def _data(content: str | bytes) -> bytes:
    return content.encode("utf-8") if isinstance(content, str) else content


def write(directory: Path, root: Path, files: dict[str, str | bytes], *,
          orders: dict[str, list[str]], notes: list[str]) -> None:
    """Record the planned write of ``files`` before any of them changes."""
    entries = {}
    for name, content in sorted(files.items()):
        path = repo_path(root, name)
        before = path.read_bytes() if path.is_file() else None
        entries[name] = {"before": None if before is None else base64.b64encode(before).decode("ascii"),
                         "after": blob_id(_data(content))}
    value = {"orders": orders, "notes": notes, "files": entries}
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, prefix=".order-batch-journal.",
                                     suffix=".tmp", delete=False) as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(stream.name, directory / JOURNAL_FILE)


def clear(directory: Path) -> None:
    (directory / JOURNAL_FILE).unlink(missing_ok=True)


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
    files = value["files"]
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
        check_write(root, name, PurePosixPath(name).parent.as_posix())  # never through a planted link
        if before is None:
            target.unlink()
        else:
            target.write_bytes(before)
    clear(directory)
