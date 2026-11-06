"""Filesystem ownership for shelves: writes, log handles, draft replacement and removal."""

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import tempfile

from foragent import shelf


@contextmanager
def _change(path: Path):
    managed = shelf.destination(path)
    if managed is None:
        yield
        return
    store, place, key = managed
    with shelf.ledger(store) as state:
        # Adopt existing files before removing them, including pre-shelf installations.
        item = store.root / key
        if not item.exists():
            shelf.transition(state, store, place, key, None)
        elif key not in state["items"].get(place, {}):
            shelf.transition(state, store, place, key, shelf._mtime(item))
        yield
        at = state["items"].get(place, {}).get(key, store._timestamp())
        shelf.transition(state, store, place, key, at if item.exists() else None)


def atomic_write(path: Path, content: str) -> None:
    """Write and fsync the temporary file here; replace the destination atomically."""
    with _change(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent,
                                             prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


@contextmanager
def open_file(path: Path, mode="a", **kwargs):
    """Open a log or subprocess redirection here and count a newly admitted file once."""
    path = Path(path)
    stream = None
    try:
        with _change(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            stream = path.open(mode, **kwargs)
        yield stream
    finally:
        if stream is not None:
            stream.close()


def append_log(path: Path, text: str) -> None:
    with open_file(path, "a", encoding="utf-8") as stream:
        stream.write(text + "\n")


def remove(path: Path) -> None:
    with _change(path):
        path.unlink(missing_ok=True)


def keep_draft(store, number: int, files: dict[str, bytes], *, log: Path | None = None) -> list[str]:
    """Replace one logical draft, including its session output, and admit it once."""
    target = store.root / "drafts" / str(number)
    with _change(target):
        staging = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        shutil.rmtree(staging, ignore_errors=True)
        try:
            for name, content in files.items():
                path = staging / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            if log is not None:
                shutil.copyfile(log, staging / "session.log")
            shutil.rmtree(target, ignore_errors=True)
            os.replace(staging, target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return sorted(files)


def prune_drafts(store) -> None:
    """Closed issues' drafts leave by their declared rule; unknown issues stay."""
    closed = {row["number"] for row in store.local_issues() if row.get("state") == "closed"}
    for path in (store.root / "drafts").glob("*"):
        if path.is_dir() and path.name.isdigit() and int(path.name) in closed:
            with _change(path):
                shutil.rmtree(path)
