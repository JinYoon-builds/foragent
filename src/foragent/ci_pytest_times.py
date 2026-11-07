"""The one writer of scripts/shard_times.json: refresh it on main when too many files are untimed.

Acceptance CI balances shards by these per-file seconds (ci_pytest.balanced).
Untimed files fall back to a path hash, which can stack heavy files on one
shard. When more than 10% of the acceptance files on main have no time, the
merge owner rewrites the file from the newest acceptance baseline JUnit report,
which run_baseline writes from a checkout of the base branch. The rewrite lands
through the main-write queue as its own commit. Without a report, the file
stays as it is and the reason is logged.
"""

from hashlib import sha256
import json
import logging
from pathlib import PurePosixPath
import xml.etree.ElementTree as ElementTree


TIMES = "scripts/shard_times.json"
ACCEPTANCE = "tests/acceptance"
REPORTS = "issue-*-acceptance-baseline.xml"
REQUESTER = "shard times"
SKIP = "shard-times-skip.txt"  # The last skip reason printed; written only here.

log = logging.getLogger(__name__)


def file_times(junit):
    """Seconds per test file, summed over the JUnit test cases of each file."""
    totals = {}
    for case in ElementTree.parse(junit).iter("testcase"):
        path = case.get("file")
        if not path:
            parts = case.get("classname", "").split(".")
            while parts and parts[-1][:1].isupper():  # drop test class names
                parts.pop()
            path = "/".join(parts) + ".py" if parts else None
        if path:
            totals[path] = totals.get(path, 0.0) + float(case.get("time") or 0)
    return {path: round(seconds, 1) for path, seconds in sorted(totals.items())}


def render(times):
    return json.dumps(times, indent=0, sort_keys=True) + "\n"


def _timed(times, name):
    value = times.get(name)
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _acceptance_files(git, ref):
    return sorted(name for name in git.tree_files(ref)
                  if name.startswith(ACCEPTANCE + "/") and PurePosixPath(name).name.startswith("test_")
                  and name.endswith(".py") and "__pycache__" not in PurePosixPath(name).parts)


def latest_report(store):
    reports = sorted((store.root / "runs").glob(REPORTS), key=lambda path: (path.stat().st_mtime, path.name))
    return reports[-1] if reports else None


def _skipped(ctx, report, reason):
    """Record why the file was not refreshed, in the owner log, once per distinct reason."""
    line = f"{TIMES} not refreshed: {reason}"
    log.info(line)
    path = ctx.store.root / SKIP
    try:
        if path.read_text(encoding="utf-8") == line:
            return None
    except OSError:
        pass
    report.actions.append(line)
    ctx.store._atomic_write(path, line)
    return None


def refresh(ctx, report):
    """Queue a rewrite of main's shard times when more than 10% of acceptance files are untimed."""
    from foragent import main_write_requests as writes
    from foragent.git import ABSENT_BLOB, GitError

    branch = ctx.config.base_branch
    try:
        tip = ctx.git.ref_sha(f"origin/{branch}")
        # The owner's blob lookup answers "does this project keep shard times?" before anything else.
        blob = ctx.git.blob_shas(tip, [TIMES])[TIMES]
        if blob == ABSENT_BLOB:
            return None  # This project keeps no shard times.
        text = ctx.git.blob_bytes(blob.encode()).decode("utf-8", "replace")
        files = _acceptance_files(ctx.git, tip)
    except GitError as exc:
        return _skipped(ctx, report, f"cannot read origin/{branch}: {exc}")
    try:
        old = json.loads(text)
    except ValueError:
        old = {}
    old = old if isinstance(old, dict) else {}
    untimed = [name for name in files if not _timed(old, name)]
    if len(untimed) * 10 <= len(files):
        (ctx.store.root / SKIP).unlink(missing_ok=True)
        return None
    if any(TIMES in (row["payload"].get("files") or {}) for row in writes.pending(ctx.store)):
        return None  # A queued rewrite lands first.
    summary = f"{len(untimed)} of {len(files)} acceptance files on {branch} untimed"
    source = latest_report(ctx.store)
    if source is None:
        return _skipped(ctx, report, f"{summary}, but no main acceptance JUnit report (runs/{REPORTS}) exists")
    try:
        measured = file_times(source)
    except (OSError, ValueError, ElementTree.ParseError) as exc:
        return _skipped(ctx, report, f"{summary}, but JUnit report {source.name} is unreadable: {exc}")
    present = set(files)
    times = {name: value for name, value in old.items() if name in present and _timed(old, name)}
    times.update({name: value for name, value in measured.items() if name in present})
    new = render(times)
    if new == text:
        return _skipped(ctx, report, f"{summary}, and JUnit report {source.name} adds no times")
    # "alone": drain publishes this request as its own commit, never batched with other writes.
    payload = {"kind": "files", "source": "shard-times", "base": tip, "alone": True,
               "files": {TIMES: {"before": writes._content(text.encode()), "after": writes._content(new.encode())}}}
    row = writes.submit(ctx.store, branch, f"Refresh {TIMES} from {source.name}", REQUESTER, payload,
                        identity=f"shard-times:{sha256(new.encode()).hexdigest()}")
    (ctx.store.root / SKIP).unlink(missing_ok=True)
    if row["status"] == "pending" and "error" not in row:
        report.actions.append(f"{TIMES}: {summary}; rewrite from JUnit {source.name} queued")
    return row
