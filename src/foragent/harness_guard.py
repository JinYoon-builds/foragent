"""Hold a head whose committed diff changes the test harness outside the issue's ## Files.

Each measurement (Done when, the acceptance comparison) first guards the exact commit or staged tree it
measures: raw Git entries from the merge base with the PR's target. The worktree is not checked (scope A).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from fnmatch import fnmatchcase
import math
from pathlib import Path
import tomllib
from typing import TYPE_CHECKING

from foragent.git import GitError
from foragent.i18n import text

if TYPE_CHECKING:
    from foragent.git import GitRepo

HARNESS_NAMES = frozenset({
    b"conftest.py", b"pytest.ini", b"tox.ini", b"setup.cfg", b"sitecustomize.py", b"usercustomize.py",
})
PYPROJECT = b"pyproject.toml"
# [tool.pytest] contains [tool.pytest.ini_options] and every other sub-table.
PYPROJECT_TABLES = (("tool", "pytest"), ("project", "entry-points", "pytest11"))
_REGULAR = (b"100644", b"100755")
_ABSENT = b"000000"
_UNPARSABLE = object()
_END = object()


def _canonical(value: object) -> tuple:
    """TOML values by type and exact value (0 is not false, nan equals nan), built without recursion."""
    tokens: list[tuple] = []
    stack: list[object] = [value]
    while stack:
        item = stack.pop()
        if item is _END:
            tokens.append(("end",))
        elif isinstance(item, tuple):
            tokens.append(item)  # ("key", name); TOML values are never tuples
        elif isinstance(item, dict):
            tokens.append(("table", len(item)))
            stack.append(_END)
            for key in sorted(item, reverse=True):
                stack.extend([item[key], ("key", key)])
        elif isinstance(item, list):
            tokens.append(("array", len(item)))
            stack.append(_END)
            stack.extend(reversed(item))
        elif isinstance(item, float):
            tokens.append(("float", "nan" if math.isnan(item) else item.hex()))
        elif hasattr(item, "isoformat"):
            tokens.append((type(item).__name__, item.isoformat()))
        else:
            tokens.append((type(item).__name__, item))
    return tuple(tokens)


def _pytest_tables(data: bytes | None) -> object:
    """The pytest tables of one side; an absent file has none."""
    try:
        document = tomllib.loads(data.decode("utf-8")) if data is not None else {}
    except Exception:  # undecodable, invalid, or nested too deeply to parse
        return _UNPARSABLE
    tables = []
    for keys in PYPROJECT_TABLES:
        value: object = document
        for key in keys:
            value = value.get(key) if isinstance(value, dict) else None
        tables.append(None if value is None else _canonical(value))
    return tables


def listed(path: bytes, files: list[str]) -> bool:
    """Whether a raw path matches a ## Files entry: the entry UTF-8 encoded, as a byte glob.

    Like policy._matches, * and ? stay within a component and ** spans
    components; nothing is normalized, so a literal entry needs equal bytes.
    """
    parts = path.split(b"/")
    for entry in files:
        positions = {0}
        for glob in entry.encode("utf-8").split(b"/"):
            if glob == b"**":
                positions = set(range(min(positions), len(parts) + 1))
            else:
                positions = {i + 1 for i in positions if i < len(parts) and fnmatchcase(parts[i], glob)}
            if not positions:
                break
        if len(parts) in positions:
            return True
    return False


def display_path(raw: bytes) -> str:
    """Valid UTF-8 as is; anything else in Git's C-quoted form."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    escapes = {7: "\\a", 8: "\\b", 9: "\\t", 10: "\\n", 11: "\\v", 12: "\\f", 13: "\\r", 34: '\\"', 92: "\\\\"}
    return '"' + "".join(escapes.get(b) or (chr(b) if 32 <= b < 127 else f"\\{b:03o}") for b in raw) + '"'


def _holds(git: GitRepo, entry: tuple[bytes, bytes, bytes, bytes, bytes], files: list[str]) -> bool:
    """Whether one committed diff entry holds the head."""
    old_mode, new_mode, old_sha, new_sha, path = entry
    parts = path.split(b"/")
    harness_dirs = any(part in HARNESS_NAMES or part == PYPROJECT for part in parts[:-1])
    name = parts[-1]
    if not harness_dirs and name not in HARNESS_NAMES and name != PYPROJECT:
        return False
    try:
        path.decode("utf-8")
    except UnicodeDecodeError:
        return True  # a Files entry is UTF-8, so such a path cannot be listed exactly
    if harness_dirs or any(mode not in (*_REGULAR, _ABSENT) for mode in (old_mode, new_mode)):
        return True  # a harness-named directory, a symlink or a submodule on either side
    try:
        # Every side Git records is read, for every harness name: a blob that
        # cannot be read cannot be compared, listed or not.
        blobs = [None if mode == _ABSENT else git.blob_bytes(sha)
                 for mode, sha in ((old_mode, old_sha), (new_mode, new_sha))]
    except GitError:
        return True
    if name == PYPROJECT:
        tables = [_pytest_tables(blob) for blob in blobs]
        if _UNPARSABLE in tables:
            return True
        if tables[0] == tables[1]:
            return False  # only the pytest tables count, so a mode-only change proceeds
    return not listed(path, files)  # an addition, deletion, content or mode change


def guard_measured(ctx, n: int, git: GitRepo, target_ref: str, measured: str, files, *,
                   tree: bool = False) -> list[str]:
    """Harness paths that hold the head about to be measured; [] when it may be measured.

    `measured` is the commit sha, or with tree=True the staged tree sha, that the
    measurement will run on. The guard compares it with the merge base of the
    target and the worktree's HEAD (with a pending MERGE_HEAD) for a tree, or of
    the target and the commit itself. A passed (merge base, measured) pair is
    recorded on the run, so repeating a measurement of an unchanged pair, or of a
    commit whose tree was guarded, does not judge it again.
    """
    if not callable(getattr(git, "committed_diff", None)):
        # A repository object that cannot read raw diffs (a test double): judge
        # its changed paths by name, failing closed; one with none has nothing to judge.
        changed_paths = getattr(git, "changed_paths", None)
        changed = [path.encode("utf-8") for path in changed_paths(target_ref)] if changed_paths else []
        return [display_path(path) for path in changed
                if any(part in HARNESS_NAMES or part == PYPROJECT for part in path.split(b"/"))]
    try:
        base = (git._own_base(target_ref) if tree
                else git._call(["merge-base", target_ref, measured]).strip())
        guarded = {measured} if tree else {measured, git._call(["rev-parse", f"{measured}^{{tree}}"]).strip()}
        record = ctx.store.get_run(n).harness_guard
        if record.get("merge_base") == base and record.get("guarded") in guarded:
            return []
        entries = git.committed_diff(base, measured)
    except GitError as exc:
        return [text("harness_unreadable", ctx.config.language, detail=" ".join(str(exc).split())[:200])]
    if callable(files):
        files = files()  # read only when there is something to judge
    held = []
    for entry in entries:
        try:
            holds = _holds(git, entry, files)
        except Exception:  # anything that stops an exact comparison holds (fail closed)
            holds = True
        if holds:
            held.append(display_path(entry[4]))
    if not held:
        ctx.store.update_run(n, harness_guard={"merge_base": base, "guarded": measured})
    return held


class HarnessHeld(Exception):
    """The guard held a head the implementation path was about to measure.

    Not an ordinary attempt failure: no retry, rebuild or replay handler catches
    it. runner._implement_and_publish publishes the hold with these details.
    """

    def __init__(self, details: str):
        super().__init__(details)
        self.details = details


_GUARDS: dict[Path, Callable[[], str | None]] = {}


@contextmanager
def guarding(root: Path, check: Callable[[], str | None]):
    """While measuring in root, before_measurement(root) runs check (a hold outcome or None)."""
    key = Path(root).resolve()
    _GUARDS[key] = check
    try:
        yield
    finally:
        _GUARDS.pop(key, None)


def before_measurement(root: Path) -> str | None:
    """The registered guard for root, run just before one measurement there."""
    check = _GUARDS.get(Path(root).resolve())
    return check() if check else None


def issue_files(ctx, n: int) -> list[str]:
    """The approved issue's ## Files entries."""
    from foragent.issue import parse_body

    approval = ctx.store.load_approval(n)
    return parse_body(approval.body).files if approval is not None else []


def review_guard(ctx, n: int, git: GitRepo, base_ref: str, files, link: str,
                 verify: Callable[[str], str | None]) -> Callable[[str], str | None]:
    """Wrap a review head's verification so the guard runs before each of its measurements."""
    def current() -> str | None:
        if held := guard_measured(ctx, n, git, base_ref, git.head_sha(), files):
            return hold(ctx, n, held, link)
        return None

    def guarded(measured: str) -> str | None:
        with guarding(git.root, current):
            return current() or verify(measured)
    return guarded


def guard_or_stop(ctx, n: int, git: GitRepo, target_ref: str, measured: str, link: str = "") -> None:
    """Guard a commit about to be measured; hold and stop the run if it changes the harness."""
    from foragent.run_attempt import ExecutorStopped

    if held := guard_measured(ctx, n, git, target_ref, measured, lambda: issue_files(ctx, n)):
        raise ExecutorStopped(hold(ctx, n, held, link), hold_details(ctx, held))


def hold_details(ctx, paths: list[str]) -> str:
    return text("harness_changed_paths", ctx.config.language) + "\n" + "\n".join(f"- `{path}`" for path in paths)


def hold(ctx, n: int, paths: list[str], link: str) -> str:
    """Flag the hold for a person at once; the caller stops before measuring."""
    from foragent.actions import flag
    from foragent.reasons import Reason

    flag(ctx, n, Reason.TEST_HARNESS_CHANGED, text("harness_changed_summary", ctx.config.language),
         f"{hold_details(ctx, paths)}\n\n{link}".rstrip(), needs_human=True)
    return "held"


def guard_staged(attempt) -> None:
    """Guard exactly the staged tree about to be measured; HarnessHeld stops the attempt."""
    ctx, git = attempt.ctx, attempt.git
    if held := guard_measured(ctx, attempt.n, git, f"origin/{attempt.target_branch}", git.staged_tree_sha(),
                              attempt.parsed.files, tree=True):
        raise HarnessHeld(hold_details(ctx, held))


def guard_comparison(attempt, head: str, pr: int | None = None) -> None:
    """Guard the head an acceptance comparison is about to measure."""
    ctx, target = attempt.ctx, getattr(attempt, "target_branch", None) or attempt.ctx.config.base_branch
    link = f"https://github.com/{ctx.repo}/pull/{pr}" if pr is not None else ""
    guard_or_stop(ctx, attempt.n, attempt.git, f"origin/{target}", head, link)


def publish_hold(attempt) -> str:
    """Publish a held head for a person (the PR when possible); no publication check overrides the hold."""
    from foragent import run_publish

    n, ctx = attempt.n, attempt.ctx
    try:
        title = f"{ctx.tracker.get_issue(n).title} (#{n})"
        run_publish.commit_exclusions(attempt, title)
        run_publish._push_branch(attempt)
        body = run_publish._render_pr_body(attempt)
        pr = ctx.tracker.find_pr(attempt.branch)
        if pr is None:
            pr = ctx.tracker.create_pr(head=attempt.branch, base=attempt.target_branch, title=title, body=body)
            attempt.pr_title = title
        else:
            run_publish._update_open_pr(attempt, pr, title, body)
        where = f"https://github.com/{ctx.repo}/pull/{pr}"
    except (run_publish.GitError, run_publish.AttemptFailure, run_publish.TrackerError) as exc:
        if isinstance(exc, run_publish.TrackerError) and exc.is_rate_limit:
            raise  # the existing GitHub wait restarts the run, and the guard holds again
        where = run_publish.text("harness_not_published", ctx.config.language,
                                 detail=" ".join(str(exc).split())[:300])
    run_publish.flag(ctx, n, run_publish.Reason.TEST_HARNESS_CHANGED,
                     run_publish.text("harness_changed_summary", ctx.config.language),
                     f"{attempt.failure}\n\n{where}", needs_human=True)
    return "held"
