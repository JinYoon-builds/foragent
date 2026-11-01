"""Local records a retry hands to its next implementer: retry default and previous-attempt summary."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from foragent import cuts, review_notes
from foragent.git import GitError

if TYPE_CHECKING:
    from foragent.executors import ExecResult
    from foragent.run_attempt import Implementation


STOP_LIMIT = 1500
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


def holds_hint(pending: str | None, message: str) -> bool:
    """Whether undelivered advice already holds ``message`` as whole blank-line-separated text.

    Messages are joined by one blank line and may hold blank lines themselves, so a
    message matches at those boundaries, not by splitting (not a substring of a sentence).
    """
    return bool(pending and message.strip()) and f"\n\n{message.rstrip()}\n\n" in f"\n\n{pending.rstrip()}\n\n"


def with_hint(pending: str | None, message: str | None) -> str | None:
    """The undelivered advice after ``message``; blank or still-pending text adds nothing."""
    if not message or not message.strip() or holds_hint(pending, message):
        return pending
    return f"{pending.rstrip()}\n\n{message}" if pending and pending.strip() else message


def add_hint(store, n: int, message: str) -> None:
    """Append ``message`` to issue ``n``'s undelivered advice, one blank line apart.

    Every producer (``fa orch retry --hint``, ``fa lanes talk --message``) only adds
    text under the state lock, so no producer can overwrite or drop another's
    undelivered message; only ``consume_hint`` removes text, after delivery. A message
    still waiting is not added twice, so the next worker reads it once.
    """
    path = hint_path(store.root, n)
    with store._state_lock():
        unused = path.read_text(encoding="utf-8") if path.is_file() else None
        updated = with_hint(unused, message)
        if updated != unused:
            store._atomic_write(path, updated)


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
            # Producers only append, so a hint not starting with the delivered text was not delivered.
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
    # The kept tail names what it left out; the whole text stays beside it.
    path.write_text(cuts.tail(text, STOP_LIMIT, cuts.issue_dir(root, n), "stop-full.txt") + "\n", encoding="utf-8")


def _lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.strip()]


def _branch_facts(attempt: Implementation) -> list[str]:
    git = attempt.git
    try:
        head = git.head_sha()
        base = git._own_base(attempt.start_ref)
        commits = _lines(git._call(["log", "--format=%h %s", f"{base}..HEAD"]))
        tracked = _lines(git._call(["diff", "--name-only", "--no-renames", base]))
        untracked = _lines(git._call(["ls-files", "--others", "--exclude-standard"]))
    except GitError:
        return [f"Branch: {attempt.branch}"]
    files = [path for path in dict.fromkeys([*tracked, *untracked]) if not path.startswith(".foragent/")]
    facts = [f"Branch: {attempt.branch} at {head[:12]}"]
    directory = cuts.issue_dir(attempt.ctx.store.root, attempt.n)
    facts.append("Commits on the branch:\n" + (cuts.head_lines("".join(f"- {line}\n" for line in commits), LIST_LIMIT,
                                                               directory, "branch-commits.txt").rstrip("\n")
                                               if commits else "- (none)"))
    facts.append("Changed files:\n" + (cuts.head_lines("".join(f"- {path}\n" for path in files), LIST_LIMIT,
                                                       directory, "branch-files.txt").rstrip("\n")
                                       if files else "- (none)"))
    return facts


def _latest_review(root: Path, n: int) -> tuple[Path, str] | None:
    rounds = [(int(path.stem.removeprefix("review-")), path)
              for path in (root / "runs" / f"issue-{n}").glob("review-*.txt")
              if path.stem.removeprefix("review-").isdigit()]
    if not rounds:
        return None
    path = max(rounds)[1]
    return path, path.read_text(encoding="utf-8", errors="replace").strip()


def saved_failures_section(root: Path, n: int, failure_text: str = "") -> str:
    """The last verification's failing commands, unless the failure text below already carries them."""
    path = cuts.failures_path(root, n)
    if not path.is_file():
        return ""
    bundle = path.read_text(encoding="utf-8", errors="replace").strip()
    if not bundle or bundle in failure_text:
        return ""
    return f"Failing checks saved by the last verification ({path}):\n{bundle}"


def saved_review_section(root: Path, n: int, failure_text: str = "") -> str:
    """The latest saved review, whole except its NOTE lines: every finding reaches the next attempt."""
    review = _latest_review(root, n)
    if review is None:
        return ""
    path, text = review
    # NOTE lines are recorded for the orchestrator; they are never findings to fix.
    text = review_notes.strip(text).strip()
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("VERDICT:")]
    if lines and all(line in failure_text for line in lines):
        return ""  # this attempt's failure text already carries every finding
    return f"Latest review findings ({path.relative_to(root)}):\n" + (text or "(empty)")


def saved_feedback_section(root: Path, n: int, failure_text: str = "") -> str:
    """Saved review and failing checks the failure text does not already carry; fresh and resumed prompts."""
    return "\n".join(part for part in (saved_review_section(root, n, failure_text),
                                        saved_failures_section(root, n, failure_text)) if part)


def previous_attempt_summary(attempt: Implementation, failure_text: str = "") -> str:
    """Short facts from Git and run artifacts; the failing output follows in its own section.

    Every way into a new attempt (retry, rerun-review, resolve, restart) reads the saved
    failure bundle here, so it never depends on a shortened run detail.
    """
    root, n = attempt.ctx.store.root, attempt.n
    facts = _branch_facts(attempt)
    stop = stop_path(root, n)
    if stop.is_file():
        facts.append("Where it stopped:\n" + stop.read_text(encoding="utf-8", errors="replace").strip())
    if saved := saved_feedback_section(root, n, failure_text):
        facts.append(saved)
    facts.append("Failed checks or findings: see below.")
    return "\n".join(facts)
