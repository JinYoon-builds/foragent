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
def _isolated_git() -> Iterator[None]:
    """Run every Git process of the review with no setting the caller brought along.

    Inherited ``GIT_*`` variables (``GIT_CONFIG_PARAMETERS``, ``GIT_CONFIG_COUNT``, ``GIT_DIR``...)
    are removed and user and system config are off, so only a repository's own config applies.
    The variables live in the process environment, so they reach the panel's and the reviewers'
    Git calls too; the caller's environment returns afterwards.
    """
    saved = {name: value for name, value in os.environ.items() if name.startswith("GIT_")}
    for name in saved:
        del os.environ[name]
    os.environ.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    try:
        yield
    finally:
        for name in [name for name in os.environ if name.startswith("GIT_")]:
            del os.environ[name]
        os.environ.update(saved)


def _scratch_checkout(source: GitRepo, head: str, temporary: Path) -> GitRepo:
    """A new repository outside the caller's worktrees holding ``head``'s tree.

    It borrows the caller's objects read-only through an alternates file and has no hooks, no
    config and no info/attributes of its own, so no hook, filter driver or fsmonitor is defined
    for any Git call in it. The caller's repository gets only read-only plumbing calls.
    """
    common = Path(source._call(["rev-parse", "--path-format=absolute", "--git-common-dir"]).strip())
    object_format = source._call(["rev-parse", "--show-object-format"]).strip()
    templates = temporary / "templates"
    templates.mkdir()
    checkout = temporary / "checkout"
    GitRepo(temporary)._call(["init", "-q", f"--template={templates}", f"--object-format={object_format}", str(checkout)])
    (checkout / ".git" / "objects" / "info").mkdir(parents=True, exist_ok=True)
    (checkout / ".git" / "objects" / "info" / "alternates").write_text(f"{common / 'objects'}\n")
    git = GitRepo(checkout)
    git._call(["update-ref", "--no-deref", "HEAD", head])
    git._call(["read-tree", head])
    git._call(["checkout-index", "--all"])
    return git


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
    with _isolated_git():
        source = GitRepo(Path.cwd())
        root = Path(source._call(["rev-parse", "--show-toplevel"]).strip())
        source = GitRepo(root)
        head, worktree_target = resolve_target(source, target)
        base_sha = source._call(["rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"]).strip()
        fork = source._call(["merge-base", head, base_sha]).strip()
        config = load_config(root)
        parsed = goal_for(root, config, goal, issue)
        directory = TemporaryDirectory(prefix="fa-review-")
        with _cleanup(directory.cleanup):
            temporary = Path(directory.name)
            git = _scratch_checkout(source, head, temporary)
            store = StateStore(temporary / "state")
            store.start_run(0, branch=target, worktree=git.root)
            ctx = LocalReviewContext(repo="", repo_root=git.root, tracker=NoPublication(), store=store,
                                     config=config, git=git, now=lambda: datetime.now(UTC))
            result = _panel_review(
                0, ctx, git, parsed, fork, resolved_reviewer(config), "",
                text("branch_review_scope", config.language), review_perspectives.shares(config), 1,
                workers=config.review_reviewers, journal=False,
            )
    return result, config.language, worktree_target
