"""Approval-backed file claims shared by scheduler admission and run boundaries."""

from dataclasses import dataclass
from datetime import datetime
from glob import escape, has_magic
from itertools import combinations, product
import json
from pathlib import Path

from foragent.git import GitError, REVIEW_SCOPE_FILE
from foragent.issue import ParsedIssue, parse_body
from foragent.state_records import Run


@dataclass
class Claim:
    run: Run
    parsed: ParsedIssue
    files: list[str]


def approved_issue(ctx, run: Run) -> ParsedIssue | None:
    approval = ctx.store.load_approval(run.number)
    if approval is None:
        return None
    if run.approval_body_sha is None:
        if datetime.fromisoformat(approval.approved_at) > datetime.fromisoformat(run.started_at):
            return None
    elif run.approval_body_sha != approval.body_sha:
        return None
    return parse_body(approval.body)


def measure(ctx, run: Run, parsed: ParsedIssue, target: str | None = None) -> Run:
    """Keep declarations until the first own commit, then include all current own edits.

    Reading the working tree includes unstaged edits, additions and deletions without
    staging anything. The merge base excludes changes inherited from the target.
    """
    fields = {"declared_files": parsed.files}
    branch = target or run.files_base_branch or ctx.config.base_branch
    if target is not None:
        fields["files_base_branch"] = target
    if not Path(run.worktree).exists():
        return run
    git = ctx.git.at(Path(run.worktree))
    try:
        base = git._own_base(f"origin/{branch}")
        committed = int(git._call(["rev-list", "--count", f"{base}..HEAD"]).strip()) > 0
        output = git._call(["diff", "--cached", "--name-only", "--no-renames", "-z", base])
        output += git._call(["diff", "--name-only", "--no-renames", "-z", base])
        output += git._call(["ls-files", "--others", "--exclude-standard", "-z"])
        excluded = {".foragent", REVIEW_SCOPE_FILE, *run.local_skill_paths, *git.local_skill_paths}
        changed = sorted(path for path in set(filter(None, output.split("\0")))
                         if not any(path == name or path.startswith(name + "/") for name in excluded))
    except (GitError, ValueError):
        # Publish none of a partial measurement, including its commit/base state.
        return run
    previous = ([escape(path) for path in run.effective_files]
                if run.files_committed and run.effective_files is not None else parsed.files)
    # Keep additions late across scheduler observations and credited-wait polls.
    # A declaration released by narrowing has to acquire its place again too.
    late = [path for path in changed if path in run.late_files
            or not any_overlap(ctx, [escape(path)], previous)
            or not any_overlap(ctx, [escape(path)], parsed.files)]
    fields.update(changed_files=changed, files_committed=committed,
                  effective_files=changed if committed else parsed.files, late_files=late)
    if all(getattr(run, name) == value for name, value in fields.items()):
        # An unchanged measurement is read-only: no state lock, no write.
        return run
    return ctx.store.update_run(run.number, **fields)


def claims(
    ctx, *, exclude: int | None = None, measured: bool = True,
) -> tuple[list[Claim], list[Run]]:
    """Active runs' file claims; ``measured=False`` uses only the recorded measurement (read-only)."""
    known, unknown = [], []
    for run in ctx.store.active_runs():
        if run.number == exclude:
            continue
        parsed = approved_issue(ctx, run)
        if parsed is None or not parsed.files:
            unknown.append(run)
            continue
        if measured:
            run = measure(ctx, run, parsed)
        known.append(claim(run, parsed))
    return known, unknown


def claim(run: Run, parsed: ParsedIssue) -> Claim:
    """A run's file claim: its committed measured files once recorded, else its approved Files."""
    files = ([escape(path) for path in run.effective_files]
             if run.files_committed and run.effective_files is not None else parsed.files)
    return Claim(run, parsed, files)


def intersects(ctx, *patterns: str) -> bool:
    from foragent import scheduler

    # A caller that must not probe passes its answer (None: unknown) as ``ctx.case_sensitive``.
    sensitive = (ctx.case_sensitive if hasattr(ctx, "case_sensitive")
                 else scheduler._filesystem_case_sensitive(ctx.repo_root))
    return sensitive is None or scheduler._file_patterns_overlap(*patterns, case_sensitive=sensitive)


def any_overlap(ctx, left: list[str], right: list[str]) -> bool:
    return any(intersects(ctx, a, b) for a in left for b in right)


def union_patterns(ctx, files: list[str], peers: list[Claim]) -> set[str]:
    """Only exempt concrete paths; a glob alone cannot prove every match is union."""
    paths = {path for path in [*files, *(path for peer in peers for path in peer.files)]
             if not has_magic(path)}
    for peer in peers:
        paths.update(peer.run.changed_files or [])
        if peer.run.files_committed:
            paths.update(peer.run.effective_files or [])
    union = ctx.git.union_merge_paths(f"origin/{ctx.config.base_branch}", paths)
    record_union(ctx.store, paths, union)
    return {escape(path) for path in union}


UNION_FILE = "union-merge-paths.json"


def record_union(store, checked: set[str], union: set[str]) -> None:
    """Keep the gate's merge=union answers, so read-only views apply the same exemptions."""
    if not checked:
        return
    path = store.root / UNION_FILE
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    known = saved.get("paths") if isinstance(saved, dict) and isinstance(saved.get("paths"), dict) else {}
    updated = {**known, **{name: name in union for name in checked}}
    if updated != known:
        store._write_json(path, {"paths": dict(sorted(updated.items()))})


def recorded_union(store) -> set[str]:
    """Escaped paths the gate last found to merge with union; unchecked paths keep their limits."""
    try:
        saved = json.loads((store.root / UNION_FILE).read_text(encoding="utf-8"))
        return {escape(name) for name, union in saved["paths"].items() if union is True}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return set()


def at_capacity(ctx, files: list[str], peers: list[Claim], *, union: set[str] | None = None) -> bool:
    """Count distinct runs sharing the same possible path, not matching patterns."""
    if union is None:
        union = union_patterns(ctx, files, peers)
    files = [path for path in files if path not in union]
    peers = [Claim(peer.run, peer.parsed, [path for path in peer.files if path not in union])
             for peer in peers]
    limit = ctx.config.overlap_per_file
    for path in files:
        matching = [peer for peer in peers if any_overlap(ctx, [path], peer.files)]
        for group in combinations(matching, limit):
            for patterns in product(*(peer.files for peer in group)):
                if intersects(ctx, path, *patterns):
                    return True
    return False


def can_start(ctx, lane: str, number: int) -> bool:
    known, unknown = claims(ctx)
    approval = ctx.store.load_approval(number)
    parsed = parse_body(approval.body) if approval is not None else None
    return admits(ctx, lane, parsed, known, unknown)


def admits(
    ctx, lane: str, parsed: ParsedIssue | None, known: list[Claim], unknown: list[Run],
    *, union: set[str] | None = None,
) -> bool:
    """The gate's file rule: lane serialisation without Files, exclusive reservations, overlap_per_file."""
    if parsed is None or not parsed.files:
        return not any(run.lane == lane for run in [*(claim.run for claim in known), *unknown])
    if any(run.lane == lane for run in unknown):
        return False
    if any(any_overlap(ctx, parsed.files + parsed.exclusive, peer.parsed.exclusive)
           or any_overlap(ctx, parsed.exclusive, peer.files) for peer in known):
        return False
    return not at_capacity(ctx, parsed.files, known, union=union)
