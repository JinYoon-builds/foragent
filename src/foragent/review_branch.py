"""A disposable first-round review of a local commit, independent of issue runs."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from foragent.config import load_config, resolved_reviewer
from foragent.git import GitError, GitRepo
from foragent.i18n import text
from foragent.issue import ParsedIssue, parse_body
from foragent import review_perspectives
from foragent.run_review import ReviewResult, _panel_review
from foragent.runner import RunContext
from foragent.state import StateStore


class ReviewError(Exception):
    """An error that is not a review verdict."""


class NoPublication:
    """Standalone reviews have no tracker capability, even on limit/error paths."""

    def __getattr__(self, name):
        raise ReviewError(text("branch_review_no_publication"))


class LocalReviewContext(RunContext):
    def __post_init__(self) -> None:
        # There is no issue tracker to wrap or global GitHub recorder to install.
        pass


@contextmanager
def _cleanup(action: Callable[[], None]) -> Iterator[None]:
    """Clean up a disposable resource without replacing an active review exception."""
    try:
        yield
    finally:
        original = sys.exception()
        try:
            action()
        except Exception as error:
            if original is None:
                raise
            original.add_note(text("branch_review_cleanup_error", error=" ".join(str(error).split())))


@contextmanager
def _no_repository_programs(source: GitRepo) -> Iterator[None]:
    """Run every Git command of this review without programs the repository configures.

    Hooks (post-checkout, reference-transaction), fsmonitor and attribute-driven filters can
    write the person's files; the review must change nothing, so Git sees no hooks directory,
    no fsmonitor and no attributes. The settings travel in the environment, so they reach the
    panel's own Git calls too, and the previous environment returns afterwards.
    """
    empty_tree = source._call(["hash-object", "-t", "tree", os.devnull]).strip()
    count = int(os.environ.get("GIT_CONFIG_COUNT", "0") or 0)
    settings = {"core.hooksPath": os.devnull, "core.fsmonitor": "false", "core.attributesFile": os.devnull}
    added = {"GIT_CONFIG_COUNT": str(count + len(settings)), "GIT_ATTR_SOURCE": empty_tree}
    for index, (key, value) in enumerate(settings.items(), start=count):
        added[f"GIT_CONFIG_KEY_{index}"] = key
        added[f"GIT_CONFIG_VALUE_{index}"] = value
    previous = {name: os.environ.get(name) for name in added}
    os.environ.update(added)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def goal_for(root: Path, config, goal: str | None, issue: int | None) -> ParsedIssue:
    if issue is None:
        return ParsedIssue(None, goal or "", "", [], [])
    repo = config.repo or GitRepo(root).remote_slug()
    rows = StateStore.for_repo(repo).local_issues()
    cached = next((row for row in rows if row["number"] == issue), None)
    if cached is None:
        # A standalone review makes no GitHub call, so an uncached issue cannot ground it.
        raise ReviewError(text("branch_review_issue_uncached", number=issue))
    parsed = parse_body(cached["body"])
    # Only the requested grounding context belongs to this independent review.
    return ParsedIssue(None, parsed.goal, parsed.out_of_scope, [], [])


def resolve_target(source: GitRepo, target: str) -> tuple[str, bool]:
    try:
        return source._call(["rev-parse", "--verify", f"refs/heads/{target}^{{commit}}"]).strip(), False
    except GitError:
        pass
    path = Path(target).expanduser()
    try:
        if path.is_dir():
            target_git = GitRepo(path.resolve())
            top = Path(target_git._call(["rev-parse", "--show-toplevel"]).strip())
            # A path must name a worktree in this repository, not another repository.
            common = Path(target_git._call(["rev-parse", "--path-format=absolute", "--git-common-dir"]).strip())
            own = Path(source._call(["rev-parse", "--path-format=absolute", "--git-common-dir"]).strip())
            if top.resolve() == path.resolve() and common.resolve() == own.resolve():
                return target_git.head_sha(), True
    except GitError:
        pass
    raise ReviewError(text("branch_review_unknown", target=target))


def review(target: str, base: str = "main", *, goal: str | None = None,
           issue: int | None = None) -> tuple[ReviewResult, str, bool]:
    source = GitRepo(Path.cwd())
    root = Path(source._call(["rev-parse", "--show-toplevel"]).strip())
    source = GitRepo(root)
    with _no_repository_programs(source):
        head, worktree_target = resolve_target(source, target)
        base_sha = source._call(["rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"]).strip()
        fork = source._call(["merge-base", head, base_sha]).strip()
        config = load_config(root)
        parsed = goal_for(root, config, goal, issue)
        directory = TemporaryDirectory(prefix="fa-review-")
        with _cleanup(directory.cleanup):
            temporary = Path(directory.name)
            checkout = temporary / "worktree"
            source._call(["worktree", "add", "--detach", str(checkout), head])
            with _cleanup(lambda: source._call(["worktree", "remove", "--force", str(checkout)])):
                git = GitRepo(checkout)
                store = StateStore(temporary / "state")
                store.start_run(0, branch=target, worktree=checkout)
                ctx = LocalReviewContext(repo="", repo_root=checkout, tracker=NoPublication(), store=store,
                                         config=config, git=git, now=lambda: datetime.now(UTC))
                result = _panel_review(
                    0, ctx, git, parsed, fork, resolved_reviewer(config), "",
                    text("branch_review_scope", config.language), review_perspectives.shares(config), 1,
                    workers=config.review_reviewers, journal=False,
                )
    return result, config.language, worktree_target
