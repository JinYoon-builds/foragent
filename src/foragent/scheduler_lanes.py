"""Lane room and file-overlap checks for starting a second clone."""

from __future__ import annotations

from pathlib import Path
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
            return not scheduler.os.path.samestat(original_stat, alternate_stat)
    except OSError:
        pass
    return None


def _file_patterns_overlap(left: str, right: str, *others: str, case_sensitive: bool = True) -> bool:
    from foragent.path_overlap import _file_patterns_overlap as intersect

    return intersect(left, right, *others, case_sensitive=case_sensitive)


def _can_start_in_lane(ctx: TickContext, lane: str, issue: Issue) -> bool:
    from foragent.overlap_files import can_start

    return can_start(ctx, lane, issue.number)
