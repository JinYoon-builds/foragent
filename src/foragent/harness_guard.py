"""Hold a head whose committed diff changes the test harness outside the issue's ## Files.

Scope (2026-10-30, option A): the guard checks only what a measured head
commits, never the worktree. Each measurement of a head (Done when, the
acceptance comparison) calls `guard_measured` with the exact commit or staged
tree it measures. The diff from the merge base with the PR's target is read as
raw Git entries (path bytes, modes, blob ids). Worktree-only tricks are left to
the reviewer and GitHub CI on a clean checkout.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
import math
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
    if name == PYPROJECT:
        try:
            tables = [_pytest_tables(None if mode == _ABSENT else git.blob_bytes(sha))
                      for mode, sha in ((old_mode, old_sha), (new_mode, new_sha))]
        except GitError:
            return True  # an unreadable blob cannot be compared
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


def issue_files(ctx, n: int) -> list[str]:
    """The approved issue's ## Files entries."""
    from foragent.issue import parse_body

    approval = ctx.store.load_approval(n)
    return parse_body(approval.body).files if approval is not None else []


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
