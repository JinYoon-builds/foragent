"""PR contents, pushes and failed-work preservation."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from foragent.git import GitRepo
    from foragent.policy import Decision
    from foragent.run_attempt import Implementation
    from foragent.runner import RunContext
    from foragent.state import Run


def _push_branch(attempt: Implementation) -> None:
    if attempt.rebuilt:
        attempt.git.push(attempt.branch, lease=attempt.rebuild_lease)
        attempt.rebuild_lease = attempt.git.head_sha()
    else:
        attempt.git.push(attempt.branch)
    attempt.pushed_head = attempt.git.head_sha()


def _skills_line(run: Run) -> str:
    """The implementer's own `Skills used:` report; every implementer is asked for one."""
    return run_publish.report_line(run.skills_used, run.skills_reported)


def _render_pr_body(attempt: Implementation, try_text: str | None = None) -> str:
    n, ctx = attempt.n, attempt.ctx
    lane = ctx.store.get_run(n).lane
    globs = ctx.config.lanes.get(lane, {}).get("paths", []) if lane != "main" else None
    inside = set(run_publish.touched(attempt.paths, globs)) if globs is not None else set(attempt.paths)
    outside = [path for path in attempt.paths if path not in inside]
    return run_publish._pr_body(
        n, attempt.parsed, attempt.checks, ctx.store.get_run(n).attempts, attempt.reviews, try_text, outside_lane=outside,
        fallback_model=ctx.store.get_run(n).fallback_model,
        attempt_tiers=ctx.store.get_run(n).attempt_tiers,
        missing_skills=attempt.missing_skills,
        skills_line=run_publish._skills_line(ctx.store.get_run(n)),
        rebuttals="\n\n".join(attempt.rebuttals),
        review_scope=ctx.config.review_scope, acceptance_dir=ctx.config.acceptance_dir,
        language=ctx.config.language,
        risk_notes=run_publish.review_perspectives.with_checklist(run_publish._risk_notes(attempt), ctx.config) if ctx.config.review_scope == "pr-body" else "",
    )


def _risk_notes(attempt: Implementation) -> str:
    paths = sorted(set(attempt.paths) | set(attempt.git.changed_paths(attempt.base_ref)))
    return run_publish.risks.review_notes(run_publish.risks.matching(run_publish.risks.load_at(attempt.git, attempt.base_ref), paths), paths)


def _merge_decision(n: int, ctx: RunContext, git: GitRepo, paths: list[str], labels) -> Decision:
    """Merge policy after first-touch deploy/migration paths join always_human (#785)."""
    run_publish.irreversible_paths.register(ctx, n, git, paths)
    return run_publish.merge_decision(changed_paths=paths, issue_labels=labels, config=ctx.config,
                                      risk_always_human=run_publish._risk_always_human(ctx))


def _risk_always_human(ctx: RunContext) -> list[str]:
    """Always-human globs from the risk map on the configured base, not the branch under review."""
    return run_publish.risks.always_human_globs(ctx.git, f"origin/{ctx.config.base_branch}")


def _open_or_update_pr(attempt: Implementation, title: str, body: str, base_sha: str | None = None) -> int:
    run_publish._check_publication(attempt.git, attempt.target_branch, base_sha)
    pr = attempt.ctx.tracker.find_pr(attempt.branch)
    if pr is None:
        pr = attempt.ctx.tracker.create_pr(head=attempt.branch, base=attempt.target_branch, title=title, body=body)
        attempt.pr_title = title
    else:
        run_publish._update_open_pr(attempt, pr, title, body)
    run_publish.board.sync_status(attempt.ctx, attempt.n, "PR open")
    return pr


def _update_open_pr(attempt: Implementation, pr: int, title: str, body: str) -> None:
    """Retarget and refresh an open PR; a failed retitle alone never stops the run.

    The merge retitles again and, if that still fails, squashes with the
    issue-derived subject (retitle_for_merge). A base that was not retargeted
    is caught by the final merge inputs, which compare the live PR base.
    """
    ctx = attempt.ctx
    try:
        ctx.tracker.update_pr(pr, base=attempt.target_branch, title=title, body=body)
    except run_publish.TrackerError as exc:
        if exc.is_rate_limit:
            raise
        attempt.pr_title = None  # unknown: the merge retitles before squashing
        run_publish._append(attempt.log, f"PR #{pr} retitle failed; retitling again at merge: {exc}")
        try:
            # Keep the current title so a rejected title alone does not block the retarget.
            current = next((item for item in ctx.tracker.list_prs("open", attempt.branch)
                            if item.number == pr), None)
            if current is not None and current.title != title:
                ctx.tracker.update_pr(pr, base=attempt.target_branch, title=current.title, body=body)
                attempt.pr_title = current.title
        except run_publish.TrackerError as retry:
            if retry.is_rate_limit:
                raise
            run_publish._append(attempt.log, f"PR #{pr} refresh under its current title failed: {retry}")
    else:
        attempt.pr_title = title


# Call-time lookups go through the facade so patches of foragent.run_publish.<name> apply.
from foragent import run_publish  # noqa: E402
