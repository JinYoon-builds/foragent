"""Classify a moved base before merging it into a verified branch (#376).

A move is result-neutral only when every new base commit touches documentation
under docs/, locked acceptance files that another open, unmerged issue owns and
this run's acceptance baseline did not run (#514), or a .foragent.yml change
limited to keys that do not affect checks (#514). Any other path, or a path
this branch changed itself, re-verifies as before.

The decision reads only the move it will merge: the fixed commit range
``head_sha..target_sha`` captured once by the caller, the exact NUL-delimited
paths of every commit in that range, and the exclusion set the acceptance
baseline actually used. The caller then merges that same ``target_sha``.
Approvals and merge history may only narrow the neutral set further.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import subprocess
from typing import TYPE_CHECKING, Any

import yaml

from foragent import labels
from foragent.config import DEFAULTS, IGNORED_KEYS, Config, ConfigError, _overlay, _validate
from foragent.git import GitError
from foragent.issue import acceptance_paths, parse_body

if TYPE_CHECKING:
    from foragent.git import GitRepo
    from foragent.run_attempt import Implementation

REUSED = "base moved: only result-neutral paths; verification reused"
REVERIFY = "base moved: result-affecting paths; re-verifying"
NEUTRAL_DIRECTORY = "docs/"
CONFIG_PATH = ".foragent.yml"
# Keys that change which checks run or who runs them (#514); others are neutral.
CHECK_KEYS = frozenset({
    "test_command", "acceptance_dir", "checks", "checks_fallback", "runner_unit_tests", "lanes", "review",
})
CHECK_PREFIXES = ("ci_", "executor", "reviewer")
APPROVAL_LABELS = frozenset({labels.APPROVED, labels.READY, labels.RUNNING})


@dataclass(frozen=True)
class BaseMove:
    """One observed base move, fixed to commit SHAs before it is judged or merged."""

    head_sha: str
    target_sha: str


def capture_move(git: GitRepo, target_sha: str) -> BaseMove:
    """Pin both ends of the range once; refs can move again before the merge."""
    return BaseMove(git.ref_sha("HEAD"), git.ref_sha(target_sha))


def fetch_move(attempt: Implementation, verified_head: str) -> BaseMove:
    """Keep the fetched base even when it is already in the verified head."""
    attempt.ctx.git.fetch("origin", attempt.target_branch)
    target = attempt.git.ref_sha(f"origin/{attempt.target_branch}")
    return BaseMove(verified_head, target)


def _git_bytes(git: GitRepo, args: list[str]) -> bytes:
    """Raw Git output; GitRepo._call decodes with replacement characters, which could
    turn two different names or an undecodable config into the same text."""
    try:
        return subprocess.run(["git", *args], cwd=git.root, check=True, capture_output=True,
                              timeout=120).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        stderr = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else None
        message = stderr.decode("utf-8", "replace") if stderr else str(exc)
        raise GitError(message) from exc


def _nul_paths(output: bytes) -> list[str]:
    # Paths are taken verbatim between NUL bytes: a filename may start or end
    # with whitespace or newlines, so no character other than NUL is stripped.
    # Bytes that are not UTF-8 stay as lone surrogates, which never classify as
    # neutral (see _decodable), instead of becoming replacement characters.
    return [path.decode("utf-8", "surrogateescape") for path in output.split(b"\0") if path != b""]


def _decodable(path: str) -> bool:
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def moved_paths(git: GitRepo, move: BaseMove) -> set[str]:
    """Paths touched by each commit in the fixed range, not only by the net diff."""
    commits = _git_bytes(git, ["rev-list", f"{move.head_sha}..{move.target_sha}"]).decode("ascii").split()
    paths: set[str] = set()
    for commit in commits:
        # -m compares merge commits with every parent; --root covers parentless
        # commits; --no-renames keeps both sides of a rename.
        paths.update(_nul_paths(_git_bytes(git, [
            "diff-tree", "-r", "-m", "--root", "--no-commit-id", "--name-only", "--no-renames", "-z",
            commit,
        ])))
    return paths


def _repo_config(git: GitRepo, sha: str) -> dict[str, Any] | None:
    """Raw repo config at one commit, or None when absent, unparsable or invalid.

    Validation uses the repository file over DEFAULTS only, never the user's
    global config, so the decision depends only on the two commits.
    """
    if git.blob_sha(sha, CONFIG_PATH) is None:
        return None
    try:
        # Strict UTF-8: undecodable bytes are unparsable, not replacement characters.
        text = _git_bytes(git, ["cat-file", "blob", f"{sha}:{CONFIG_PATH}"]).decode("utf-8")
        value = yaml.safe_load(text)
    except (GitError, UnicodeDecodeError, yaml.YAMLError):
        return None
    value = {} if value is None else value
    if not isinstance(value, dict) or any(key not in DEFAULTS and key not in IGNORED_KEYS for key in value):
        return None
    if "max_parallel" in value and value["max_parallel"] is None:
        return None
    values = deepcopy(DEFAULTS)
    try:
        _overlay(values, value)
        _validate(values)
        Config(**values)
    except (ConfigError, TypeError, ValueError):
        return None
    return value


def config_move_is_neutral(git: GitRepo, move: BaseMove) -> bool:
    """Compare raw keys between the verified head and the target only."""
    before, after = _repo_config(git, move.head_sha), _repo_config(git, move.target_sha)
    if before is None or after is None:
        return False
    missing = object()
    changed = {key for key in before.keys() | after.keys()
               if before.get(key, missing) != after.get(key, missing)}
    return not any(key in CHECK_KEYS or key.startswith(CHECK_PREFIXES) for key in changed)


def _repo_path(path: str) -> str:
    """Canonical repository-relative form, as acceptance_paths() normalizes it."""
    while path.startswith("./"):
        path = path[2:]
    return path.rstrip("/")


def _open_acceptance_owners(attempt: Implementation) -> dict[str, set[int]]:
    """Locked path -> open, unmerged issues that own it, this one included (#514).

    Owners come from approval snapshots of open issues, or from open issues
    carrying an approval label whose Files or acceptance links name the path.
    """
    ctx = attempt.ctx
    directory = _repo_path(ctx.config.acceptance_dir) + "/"
    merged = {run.number for run in ctx.store.read_history() if run.outcome == "merged"}
    issues = {issue.number: issue for issue in ctx.tracker.list_issues("open")}
    owners: dict[str, set[int]] = {}

    def own(name: str, number: int) -> None:
        # Git reports canonical paths; config and issue text may say ./dir/.
        name = _repo_path(name)
        if name.startswith(directory) and number not in merged:
            owners.setdefault(name, set()).add(number)

    for path in (ctx.store.root / "approvals").glob("*.json"):
        approval = ctx.store.load_approval(int(path.stem))
        if approval is not None and approval.number in issues:
            for name in approval.acceptance_blobs:
                own(name, approval.number)
    for issue in issues.values():
        if issue.labels & APPROVAL_LABELS:
            parsed = parse_body(issue.body)
            for name in [*parsed.files, *acceptance_paths(parsed.done_when_commands, ctx.config.acceptance_dir)]:
                own(name, issue.number)
    return owners


def base_move_is_neutral(attempt: Implementation, move: BaseMove) -> bool:
    paths = moved_paths(attempt.git, move)
    if not all(_decodable(path) for path in paths):
        return False
    own = set(attempt.git.changed_paths(move.target_sha))
    if paths & own:
        return False
    rest = {path for path in paths if not path.startswith(NEUTRAL_DIRECTORY)}
    if CONFIG_PATH in rest:
        if not config_move_is_neutral(attempt.git, move):
            return False
        rest.discard(CONFIG_PATH)
    if not rest:
        return True
    # Only files the verified baseline did not run can be neutral: those it
    # skipped, or those missing from the commit it ran on (#514). An earlier
    # neutral move may already have brought such a file into the verified head.
    # An issue approved later does not remove a test from what was already verified.
    skipped = set(attempt.baseline.excluded) if attempt.baseline else set()
    ran_on = (attempt.baseline.base_sha if attempt.baseline else None) or move.head_sha
    unverified = {path for path in rest
                  if path in skipped or attempt.git.blob_sha(ran_on, path) is None}
    # This issue's own locked files are verified by Done when.
    if not rest <= unverified - set(attempt.approval.acceptance_blobs):
        return False
    owners = _open_acceptance_owners(attempt)
    return all(owners.get(path) and attempt.n not in owners[path] for path in rest)
