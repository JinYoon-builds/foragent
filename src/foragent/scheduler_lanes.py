"""Lane room and file-overlap checks for starting a second clone."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from fnmatch import translate
from glob import has_magic
import os
from pathlib import Path, PurePosixPath
import re
from typing import TYPE_CHECKING

from foragent import scheduler
from foragent.tracker import Issue

if TYPE_CHECKING:
    from foragent.scheduler import TickContext

def _filesystem_case_sensitive(repo_root: Path) -> bool | None:
    """Probe an existing root entry without creating or modifying any files."""
    try:
        for entry in repo_root.iterdir():
            alternate = entry.with_name(entry.name.swapcase())
            if alternate == entry:
                continue
            try:
                original_stat = entry.stat()
                try:
                    alternate_stat = alternate.stat()
                except FileNotFoundError:
                    return True
            except OSError:
                continue
            return not os.path.samestat(original_stat, alternate_stat)
    except OSError:
        pass
    return None


def _file_patterns_overlap(left: str, right: str, *, case_sensitive: bool = True) -> bool:
    """Intersect path globs, including files that do not exist yet.

    As in policy._matches, * and ? stay in a component; ** spans components.
    Walking pairs of glob positions accounts for both patterns' wildcards.
    """
    def intersect(a, b, star: str, compatible: Callable[[str, str], bool]) -> bool:
        pending, seen = [(0, 0)], set()
        while pending:
            i, j = pending.pop()
            if (i, j) in seen:
                continue
            seen.add((i, j))
            if i == len(a) and j == len(b):
                return True
            if i < len(a) and a[i] == star:
                pending.append((i + 1, j))
            if j < len(b) and b[j] == star:
                pending.append((i, j + 1))
            if i < len(a) and j < len(b) and compatible(a[i], b[j]):
                pending.append((i + (a[i] != star), j + (b[j] != star)))
        return False

    def tokens(pattern: str):
        i = 0
        while i < len(pattern):
            start, i = i, i + 1
            if pattern[start] == "[":
                end = i
                if end < len(pattern) and pattern[end] == "!":
                    end += 1
                if end < len(pattern) and pattern[end] == "]":
                    end += 1
                while end < len(pattern) and pattern[end] != "]":
                    end += 1
                if end < len(pattern):
                    i = end + 1
            yield pattern[start:i]

    def components_overlap(a: str, b: str) -> bool:
        # Full case folding covers literal filesystem aliases such as ß/ss.
        # Leave globs unchanged so ? and character classes keep their width.
        if (
            not case_sensitive and not (a + b).isascii()
            and not has_magic(a) and not has_magic(b) and a.casefold() == b.casefold()
        ):
            return True
        # Literal/range endpoints and their successors represent every distinct
        # character class membership when matching case-sensitively.
        alphabet = {"\x01"} | {
            chr(code) for char in a + b for code in (ord(char), ord(char) + 1)
            if 0 < code <= 0x10ffff and code != ord("/")
        }
        if not case_sensitive:
            alphabet.update(chr(code) for code in range(1, 128) if code != ord("/"))
        a_tokens, b_tokens = tuple(tokens(a)), tuple(tokens(b))
        # Apply case equivalence to each token, preserving its width and the
        # original character classes (including punctuation inside [A-z]).
        matchers = {
            token: re.compile(translate(token), 0 if case_sensitive else re.IGNORECASE)
            for token in set(a_tokens + b_tokens)
        }

        def compatible(x: str, y: str) -> bool:
            if any(matchers[x].fullmatch(c) and matchers[y].fullmatch(c) for c in alphabet):
                return True
            # Unicode case mappings inside classes need not be monotonic, so
            # endpoints alone cannot prove two nonliteral tokens disjoint.
            return (
                not case_sensitive and not (x + y).isascii()
                and (len(x) > 1 or x in ("*", "?"))
                and (len(y) > 1 or y in ("*", "?"))
            )

        return intersect(
            a_tokens, b_tokens, "*", compatible,
        )

    return intersect(PurePosixPath(left).parts, PurePosixPath(right).parts, "**", components_overlap)


def _lane_has_room(running: int, waiting: int) -> bool:
    """A lane runs one issue, or a second clone while two or more issues wait."""
    return running == 0 or (running == 1 and waiting >= 2)


def _can_start_in_lane(ctx: TickContext, lane: str, issue: Issue, waiting: int) -> bool:
    running = [run for run in ctx.store.active_runs() if run.lane == lane]
    if not running:
        return True
    if not scheduler._lane_has_room(len(running), waiting):
        return False
    approval = ctx.store.load_approval(issue.number)
    run = running[0]
    running_approval = ctx.store.load_approval(run.number)
    if approval is None or running_approval is None:
        return False
    # The approval file can be replaced while the runner retains its old body.
    # Legacy records can use a snapshot approved no later than the run start.
    if run.approval_body_sha is None:
        if (
            datetime.fromisoformat(running_approval.approved_at)
            > datetime.fromisoformat(run.started_at)
        ):
            return False
    elif run.approval_body_sha != running_approval.body_sha:
        return False
    files = scheduler.parse_body(approval.body).files
    running_files = scheduler.parse_body(running_approval.body).files
    if not files or not running_files:
        return False
    case_sensitive = scheduler._filesystem_case_sensitive(ctx.repo_root)
    return case_sensitive is not None and not any(
        scheduler._file_patterns_overlap(candidate, active, case_sensitive=case_sensitive)
        for candidate in files for active in running_files
    )
