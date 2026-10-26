"""The one-time request to star Foragent, shown only in the human `fa brief`.

The record lives in machine data (``star-prompt.json``), so it is shared by
every repository on this machine. Showing the question records it as shown;
any answer other than a successful ``yes`` makes it final.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Iterator

from foragent import capacity, gh
from foragent.brief import STAR_PROMPT_MERGE_THRESHOLD
from foragent.i18n import text

FORAGENT_REPO = "JinYoon-builds/foragent"
RECORD = "star-prompt.json"


def _path() -> Path:
    return capacity.machine_root() / RECORD


def _read() -> dict[str, Any] | None:
    try:
        value = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write(value: dict[str, Any], *, exclusive: bool = False) -> bool:
    """Write the record atomically; ``exclusive`` only creates it, returning False if it exists."""
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=".star-prompt-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value) + "\n")
        if not exclusive:
            temporary.replace(path)
            return True
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _answer_lock() -> Iterator[None]:
    """Serialise answers on this machine so a failed yes never undoes another final answer."""
    path = _path().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def local_merges(root: Path | None = None) -> int:
    """Distinct issues each repository's gate merged on this machine."""
    root = root or capacity.machine_root()
    merged: set[tuple[str, int]] = set()
    for history in root.glob("*/runs.jsonl"):
        try:
            lines = history.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                run = json.loads(line)
            except ValueError:
                continue
            if isinstance(run, dict) and run.get("outcome") == "merged" and isinstance(run.get("number"), int):
                merged.add((history.parent.name, run["number"]))
    return len(merged)


def _gh_ready() -> bool:
    try:
        result = gh.run(["auth", "status"], capture_output=True, text=True, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _eligible() -> bool:
    """Never shown yet, enough merges, and gh can star."""
    return (_read() is None and local_merges() >= STAR_PROMPT_MERGE_THRESHOLD and _gh_ready())


def offer(language: str, now: datetime | None = None) -> str | None:
    """The question line, recorded as shown before it is returned; None when not due."""
    if not _eligible():
        return None
    # Only the invocation that creates the record shows the question, even when
    # several `fa brief` runs on this machine become eligible at the same time.
    if not _write({"state": "shown", "shown_at": (now or datetime.now(UTC)).isoformat()}, exclusive=True):
        return None
    return text("star_question", language, repo=FORAGENT_REPO)


def answer(choice: str, language: str, now: datetime | None = None) -> str:
    """Record the answer to a shown (or due) question; only ``yes`` stars Foragent."""
    with _answer_lock():
        return _answer(choice, language, now)


def _answer(choice: str, language: str, now: datetime | None) -> str:
    record = _read()
    if not ((record is not None and record.get("state") == "shown") or _eligible()):
        return text("star_not_asked", language)
    at = (now or datetime.now(UTC)).isoformat()
    if choice != "yes":
        _write({"state": "answered", "answer": choice, "answered_at": at})
        return text("star_declined", language)
    if not _gh_ready():
        # Not eligible while gh cannot star: ignore the answer and keep the record as it is.
        return text("star_not_asked", language)
    try:
        gh.run(["api", "-X", "PUT", f"user/starred/{FORAGENT_REPO}"],
               capture_output=True, text=True, check=True)
    except (OSError, subprocess.SubprocessError):
        # Not recorded: the question may be shown again. Answers hold the lock,
        # so only the still-open shown record seen above is removed here.
        if record is not None and _read() == record:
            _path().unlink(missing_ok=True)
        return text("star_failed", language)
    _write({"state": "answered", "answer": "yes", "answered_at": at})
    return text("star_thanks", language, repo=FORAGENT_REPO)
