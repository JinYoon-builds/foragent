"""Wide issues merge first (#2353): a narrower issue on a wide issue's files waits for its merge.

A wide issue is an approved open issue whose approved Files list more paths than the
split-wide-issues threshold (``issue_max_files``, default 10). While it is in force (running,
ready, or waiting for its merge), an issue that needs one of its files does not start, unless
the global priority list puts that issue before the wide one. A wide issue that is blocked,
needs a person, is parked, waits on an open prerequisite, or closed holds nothing.
Everything here reads local records only: no GitHub call, no write.
"""

from foragent import orch_queue
from foragent.issue import ParsedIssue, parse_body
from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, WAITING_PERSON

DEFAULT_MAX_FILES = 10  # split-wide-issues' default; the setting is Config.issue_max_files.
STOPPED = frozenset({BLOCKED, NEEDS_HUMAN, WAITING_PERSON})


def max_files(config) -> int:
    return getattr(config, "issue_max_files", DEFAULT_MAX_FILES)


def _in_force(ctx, row: dict) -> ParsedIssue | None:
    """The wide issue's approved Files when it holds narrower issues now, else None."""
    if row.get("state") != "open" or row.get("is_pull_request"):
        return None
    labels = set(row.get("labels") or ())
    if APPROVED not in labels or labels & STOPPED:
        return None
    if any(edge.get("state") == "open" for edge in row.get("blocked_by") or ()):
        return None
    approval = ctx.store.load_approval(row["number"])
    if approval is None:
        return None
    parsed = parse_body(approval.body)
    return parsed if len(parsed.files) > max_files(ctx.config) else None


def _first(priority: list[int], number: int, other: int) -> bool:
    """Whether the global priority list puts ``number`` before ``other``."""
    if number not in priority:
        return False
    return other not in priority or priority.index(number) < priority.index(other)


def holds(ctx, number: int, parsed: ParsedIssue | None) -> list[dict]:
    """The wide issues that make ``number`` wait, one hold per shared file; empty when none does."""
    from foragent.overlap_files import any_overlap

    if parsed is None or not parsed.files:
        return []
    priority = orch_queue.get_global(ctx.store)
    runs = {run.number: run for run in ctx.store.active_runs() if run.kind == "issue"}
    found: dict[str, list[int]] = {}
    for row in ctx.store.local_issues():
        wide = row["number"]
        if wide == number or (files := _in_force(ctx, row)) is None:
            continue
        # Between two wide issues the wider one goes first, then the older one.
        if (len(parsed.files), -number) >= (len(files.files), -wide):
            continue
        if _first(priority, number, wide):
            continue
        for path in parsed.files:
            if any_overlap(ctx, [path], files.files):
                found.setdefault(path, []).append(wide)
    return [{"file": path, "limit": None, "cause": "wide-first",
             "peers": [{"issue": wide, "run_started_at": runs[wide].started_at if wide in runs else None}
                       for wide in sorted(set(found[path]))]}
            for path in sorted(found)]
