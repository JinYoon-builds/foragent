"""Capture and cache the passing acceptance set on the selected base."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
from typing import TYPE_CHECKING
import xml.etree.ElementTree as ET

from foragent.run_attempt import AttemptFailure
from foragent.run_checks import _append, _passing_acceptance_ids, _pytest_command, junit_node_id
from foragent.run_waits import _credited_wait, _phase, _progress, _test_slot
from foragent.state import StateStore

if TYPE_CHECKING:
    from foragent.runner import RunContext


@dataclass(frozen=True)
class AcceptanceBaseline:
    command: str
    node_ids: tuple[str, ...]


def _baseline_environment(root: Path) -> dict:
    locks = [root / name for name in ("uv.lock", "poetry.lock", "pdm.lock", "Pipfile.lock", "pylock.toml")]
    manifests = [path for path in locks if path.is_file()]
    if not manifests and (root / "pyproject.toml").is_file():
        manifests = [root / "pyproject.toml"]
    return {
        "python": sys.version,
        "dependencies": {path.name: sha256(path.read_bytes()).hexdigest() for path in manifests},
        "pytest_env": {name: value for name, value in os.environ.items() if name.startswith("PYTEST_")},
    }


def _capture_acceptance_baseline(
    n: int, ctx: RunContext, test_command: str | None, target_ref: str,
) -> AcceptanceBaseline | None:
    from foragent import runner

    log = ctx.store.log_path(n)
    command = _pytest_command(test_command) if test_command else None
    if command is None:
        reason = "no test command is known" if test_command is None else f"not a supported pytest command: {test_command}"
        _append(log, f"Acceptance baseline skipped (count=0): {reason}")
        return None
    base_sha = ctx.git.ref_sha(target_ref)
    merged = {run.number for run in ctx.store.read_history() if run.outcome == "merged"}
    owners: dict[str, set[int]] = {}
    for path in (ctx.store.root / "approvals").glob("*.json"):
        approval = ctx.store.load_approval(int(path.stem))
        if approval is None or approval.number in merged:
            continue
        for name in approval.acceptance_blobs:
            owners.setdefault(name, set()).add(approval.number)
    excluded = sorted(name for name in owners if ctx.git.blob_sha(base_sha, name) is not None)
    open_issues = sorted({number for name in excluded for number in owners[name]})
    _append(log, f"Acceptance baseline skipped {len(excluded)} files owned by open issues: "
                 + " ".join(f"#{number}" for number in open_issues))
    key = sha256(json.dumps(
        [base_sha, test_command, ctx.config.acceptance_dir, _baseline_environment(ctx.repo_root), excluded],
        sort_keys=True,
    ).encode()).hexdigest()
    # Repository stores share this parent (normally ~/.local/share/foragent).
    cache = StateStore(ctx.store.root.parent / "baselines")
    cache.root.mkdir(parents=True, exist_ok=True)
    cached_ids = cache.root / f"{key}.json"
    with (cache.root / f"{key}.lock").open("a", encoding="utf-8") as lock:
        _phase(n, ctx, "checking")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Another run builds this baseline (possibly queued for a test slot); not our run time.
            _credited_wait(n, ctx, lambda: fcntl.flock(lock.fileno(), fcntl.LOCK_EX))
        try:
            saved = json.loads(cached_ids.read_text(encoding="utf-8"))
            saved_ids = saved["node_ids"]
            age = ctx.now() - datetime.fromisoformat(saved["created_at"])
        except (OSError, ValueError, TypeError, KeyError):
            pass
        else:
            if (timedelta() <= age < timedelta(hours=6) and isinstance(saved_ids, list)
                    and all(isinstance(node_id, str) for node_id in saved_ids)):
                _append(log, f"Acceptance baseline count={len(saved_ids)}, cache=hit")
                return AcceptanceBaseline(command, tuple(saved_ids))
        report = ctx.store.root / "runs" / f"issue-{n}-acceptance-baseline.xml"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.unlink(missing_ok=True)
        # Use a fresh base checkout; tests can write files without touching the implementation.
        with tempfile.TemporaryDirectory(prefix="foragent-baseline-") as directory:
            worktree = Path(directory) / "worktree"
            ctx.git.add_detached_worktree(worktree, base_sha)
            try:
                if not (worktree / ctx.config.acceptance_dir).is_dir():
                    _append(log, "Acceptance baseline skipped (count=0): acceptance directory does not exist")
                    return None
                if runner._remaining(n, ctx) <= 0:
                    raise AttemptFailure("acceptance baseline unavailable: run budget exhausted before capture")
                selection = f"{command} {shlex.quote(ctx.config.acceptance_dir)}"
                selection += "".join(f" {shlex.quote('--ignore=' + name)}" for name in excluded)
                baseline_command = (
                    f"{selection} "
                    f"-o junit_family=legacy --junit-prefix= --junitxml={shlex.quote(str(report.resolve()))}"
                )
                offset = log.stat().st_size if log.exists() else 0
                result = runner._check(baseline_command, worktree, log, min(900, runner._remaining(n, ctx)),
                                progress=lambda: _progress(n, ctx), slot=_test_slot(n, ctx))
                _progress(n, ctx)
                # Read this execution's collection count from the full log: a long
                # traceback can push it out of CheckResult's bounded output tail.
                collected = None
                cacheable = False
                try:
                    with log.open(encoding="utf-8", errors="replace") as stream:
                        stream.seek(offset)
                        for line in stream:
                            if count := re.match(r"collected (\d+) items?\b", line):
                                collected = int(count[1])
                                break
                except OSError:
                    pass
                try:
                    node_ids = _passing_acceptance_ids(report)
                    junit = ET.parse(report)
                    # A failed call and failed teardown produce two records for
                    # one node. Count each executed node once, across all outcomes.
                    recorded = len({(case.get("file"), case.get("classname"), case.get("name"))
                                    for case in junit.iter("testcase")})
                    # Exit 1 without a reported test failure/error is a session
                    # failure, even when the terminal reporter is disabled.
                    expected_exit = int(any(case.find(tag) is not None
                                            for case in junit.iter("testcase")
                                            for tag in ("failure", "error")))
                    # Ignoring all locked files can leave a valid, empty suite.
                    if recorded == 0 and result.exit_code == 5:
                        expected_exit = 5
                    # --continue-on-collection-errors can make collection failures exit 1.
                    collection_errors = junit.find(".//error[@message='collection failure']") is not None
                    usable_report = (junit.getroot().tag in {"testsuites", "testsuite"}
                                     and all(junit_node_id(case) is not None for case in junit.iter("testcase")))
                except (OSError, ET.ParseError) as exc:
                    _append(log, f"Acceptance baseline count=0: no usable JUnit report ({exc})")
                    node_ids = ()
                else:
                    # JUnit and the exit status are the subprocess contract;
                    # terminal collection output is an optional cross-check.
                    cacheable = (usable_report and not result.timed_out
                            and result.exit_code == expected_exit and not collection_errors
                            and (collected is None or collected == recorded))
                    if cacheable:
                        cache._atomic_write(cached_ids, json.dumps({
                            "node_ids": node_ids, "created_at": ctx.now().isoformat(),
                        }) + "\n")
                    else:
                        _append(log, f"Acceptance baseline not cached: collected={collected}, recorded={recorded}")
                _append(log, f"Acceptance baseline count={len(node_ids)}, exit_code={result.exit_code}, timed_out={result.timed_out}")
                if not cacheable:
                    raise AttemptFailure(
                        f"acceptance baseline unavailable: exit_code={result.exit_code}, timed_out={result.timed_out}; "
                        "capture did not produce a complete, usable JUnit report"
                    )
                return AcceptanceBaseline(command, node_ids)
            finally:
                ctx.git.remove_worktree(worktree)
