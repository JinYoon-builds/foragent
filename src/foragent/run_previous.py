"""Local records a retry hands to its next implementer: retry default and previous-attempt summary."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from foragent.git import GitError

if TYPE_CHECKING:
    from foragent.executors import ExecResult
    from foragent.run_attempt import Implementation


STOP_LIMIT = 1500
REVIEW_LIMIT = 3000
LIST_LIMIT = 50


def hint_path(root: Path, n: int) -> Path:
    return root / "hints" / f"{n}.txt"


def retry_marker(root: Path, n: int) -> Path:
    """Present after a queued retry until the next implementer receives the retry default."""
    return root / "hints" / f"{n}.retry"


def pending_hint(root: Path, n: int) -> tuple[bool, str | None]:
    """Whether a queued retry's default is pending, and the orchestrator's own hint."""
    path = hint_path(root, n)
    hint = path.read_text(encoding="utf-8") if path.is_file() else None
    return retry_marker(root, n).is_file(), hint


def consume_hint(root: Path, n: int, used: str | None) -> None:
    """Acknowledge the delivered hint ``used``; text added after it was read stays for the next attempt.

    `fa lanes talk` appends a message to an unused hint (one blank line apart)
    while an attempt runs, so the file may now hold more than the prompt did.
    """
    from foragent.state_locks import _file_lock

    path = hint_path(root, n)
    with _file_lock(root / "state.lock"):  # The lock fa lanes talk appends under.
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current is not None and used is not None:
            delivered = used.rstrip()
            if current.rstrip() == delivered:
                path.unlink(missing_ok=True)
            elif current.startswith(delivered):
                remainder = current[len(delivered):].lstrip("\n")
                if remainder.strip():
                    temporary = path.with_name(path.name + ".tmp")
                    temporary.write_text(remainder, encoding="utf-8")
                    temporary.replace(path)
                else:
                    path.unlink(missing_ok=True)
            # Otherwise someone replaced the hint (orch retry); it was not delivered.
        retry_marker(root, n).unlink(missing_ok=True)


def stop_path(root: Path, n: int) -> Path:
    return root / "runs" / f"issue-{n}-stop.txt"


def record_stop_point(root: Path, n: int, result: ExecResult | None, log: Path, offset: int) -> None:
    """Keep where the implementer stopped: its final message, else this turn's log output.

    Without a result (limit waits, launch failures, interruptions) the turn's log output is used.
    """
    text = ""
    if result is not None:
        text = (result.full_message if result.full_message is not None else result.last_message) or ""
    if not text.strip() and log.is_file():
        with log.open("rb") as stream:
            stream.seek(offset)
            text = stream.read().decode("utf-8", errors="replace")
    text = text.strip()
    if not text:
        return
    path = stop_path(root, n)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text[-STOP_LIMIT:] + "\n", encoding="utf-8")


def _lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.strip()]


def _branch_facts(attempt: Implementation) -> list[str]:
    git = attempt.git
    try:
        head = git.head_sha()
        base = git._own_base(attempt.start_ref)
        commits = _lines(git._call(["log", "--format=%h %s", f"-n{LIST_LIMIT}", f"{base}..HEAD"]))
        tracked = _lines(git._call(["diff", "--name-only", "--no-renames", base]))
        untracked = _lines(git._call(["ls-files", "--others", "--exclude-standard"]))
    except GitError:
        return [f"Branch: {attempt.branch}"]
    files = [path for path in dict.fromkeys([*tracked, *untracked]) if not path.startswith(".foragent/")]
    facts = [f"Branch: {attempt.branch} at {head[:12]}"]
    facts.append("Commits on the branch:\n" + ("\n".join(f"- {line}" for line in commits) if commits else "- (none)"))
    facts.append("Changed files:\n" + ("\n".join(f"- {path}" for path in files[:LIST_LIMIT]) if files else "- (none)")
                 + (f"\n- … {len(files) - LIST_LIMIT} more" if len(files) > LIST_LIMIT else ""))
    return facts


def _latest_review(root: Path, n: int) -> tuple[Path, str] | None:
    rounds = [(int(path.stem.removeprefix("review-")), path)
              for path in (root / "runs" / f"issue-{n}").glob("review-*.txt")
              if path.stem.removeprefix("review-").isdigit()]
    if not rounds:
        return None
    path = max(rounds)[1]
    return path, path.read_text(encoding="utf-8", errors="replace").strip()


def previous_attempt_summary(attempt: Implementation) -> str:
    """Short facts from Git and run artifacts; the failing output follows in its own section."""
    root, n = attempt.ctx.store.root, attempt.n
    facts = _branch_facts(attempt)
    if review := _latest_review(root, n):
        path, text = review
        facts.append(f"Latest review findings ({path.relative_to(root)}):\n" + (text[-REVIEW_LIMIT:] or "(empty)"))
    stop = stop_path(root, n)
    if stop.is_file():
        facts.append("Where it stopped:\n" + stop.read_text(encoding="utf-8", errors="replace").strip())
    facts.append("Failed checks or findings: see below.")
    return "\n".join(facts)
