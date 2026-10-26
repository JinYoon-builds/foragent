"""Approval-backed file claims shared by scheduler admission and run boundaries."""

from dataclasses import dataclass
from datetime import datetime
from glob import escape
from itertools import combinations, product
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
    return ctx.store.update_run(run.number, **fields)


def claims(ctx, *, exclude: int | None = None) -> tuple[list[Claim], list[Run]]:
    known, unknown = [], []
    for run in ctx.store.active_runs():
        if run.number == exclude:
            continue
        parsed = approved_issue(ctx, run)
        if parsed is None or not parsed.files:
            unknown.append(run)
            continue
        run = measure(ctx, run, parsed)
        files = ([escape(path) for path in run.effective_files]
                 if run.files_committed and run.effective_files is not None else parsed.files)
        known.append(Claim(run, parsed, files))
    return known, unknown


def intersects(ctx, *patterns: str) -> bool:
    from foragent import scheduler

    sensitive = scheduler._filesystem_case_sensitive(ctx.repo_root)
    return sensitive is None or scheduler._file_patterns_overlap(*patterns, case_sensitive=sensitive)


def any_overlap(ctx, left: list[str], right: list[str]) -> bool:
    return any(intersects(ctx, a, b) for a in left for b in right)


def at_capacity(ctx, files: list[str], peers: list[Claim]) -> bool:
    """Count distinct runs sharing the same possible path, not matching patterns."""
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
    if parsed is None or not parsed.files:
        return not any(run.lane == lane for run in [*(claim.run for claim in known), *unknown])
    if any(run.lane == lane for run in unknown):
        return False
    if any(any_overlap(ctx, parsed.files + parsed.exclusive, peer.parsed.exclusive)
           or any_overlap(ctx, parsed.exclusive, peer.files) for peer in known):
        return False
    return not at_capacity(ctx, parsed.files, known)
