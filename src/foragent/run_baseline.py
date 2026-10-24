"""Capture and cache the passing acceptance set on the selected base."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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
from foragent.run_checks import CheckResult, _append, _passing_acceptance_ids, _pytest_command, junit_node_id
from foragent.run_waits import _credited_wait, _phase, _progress, _test_slot
from foragent.state import StateStore

if TYPE_CHECKING:
    from foragent.runner import RunContext


@dataclass(frozen=True)
class AcceptanceBaseline:
    command: str
    node_ids: tuple[str, ...]
    collected_ids: tuple[str, ...] | None = None
    failing_ids: tuple[str, ...] | None = None


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


def _baseline_exclusions(ctx: RunContext, base_sha: str, log: Path) -> list[str]:
    """Locked files owned by approved issues that have not merged (#327)."""
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
    return excluded


def _baseline_identity(ctx: RunContext, test_command: str, base_sha: str) -> list:
    return [base_sha, test_command, ctx.config.acceptance_dir, _baseline_environment(ctx.repo_root)]


def _baseline_cache(ctx: RunContext, test_command: str, base_sha: str,
                    excluded: list[str]) -> tuple[StateStore, str]:
    key = sha256(json.dumps([*_baseline_identity(ctx, test_command, base_sha), excluded],
                           sort_keys=True).encode()).hexdigest()
    # Repository stores share this parent (normally ~/.local/share/foragent).
    return StateStore(ctx.store.root.parent / "baselines", now=ctx.now), key


def _premerge_baseline(ctx: RunContext, test_command: str, base_sha: str,
                       excluded: list[str], required_ids: frozenset[str]) -> dict | None:
    """Reuse a baseline only where it covers the head failures being compared.

    Stores can have different approvals for the same base. Exclusions change
    coverage, not the recorded outcomes of the other tests on that commit.
    """
    cache, key = _baseline_cache(ctx, test_command, base_sha, excluded)
    scope = sha256(json.dumps(_baseline_identity(ctx, test_command, base_sha), sort_keys=True).encode()).hexdigest()
    for candidate in [cache.root / f"{key}.json", *sorted(cache.root.glob("*.json"))]:
        saved = cache.load_acceptance_baseline(candidate.stem)
        if saved is None or not {"collected_ids", "failing_ids", "excluded"} <= saved.keys():
            continue
        if candidate.stem != key and saved.get("scope") != scope:
            continue
        if any(node.split("::", 1)[0] in saved["excluded"] for node in required_ids):
            continue
        return saved
    return None


def _saved_baseline(command: str, saved: dict) -> AcceptanceBaseline:
    return AcceptanceBaseline(command, tuple(saved["node_ids"]),
                              tuple(saved["collected_ids"]) if "collected_ids" in saved else None,
                              tuple(saved["failing_ids"]) if "failing_ids" in saved else None)


def _capture_acceptance_baseline(
    n: int, ctx: RunContext, test_command: str | None, target_ref: str,
) -> AcceptanceBaseline | None:
    from foragent import runner

    def check(command: str, worktree: Path) -> CheckResult:
        if runner._remaining(n, ctx) <= 0:
            raise AttemptFailure("acceptance baseline unavailable: run budget exhausted before capture")
        result = runner._check(command, worktree, ctx.store.log_path(n), min(900, runner._remaining(n, ctx)),
                               progress=lambda: _progress(n, ctx), slot=_test_slot(n, ctx))
        _progress(n, ctx)
        return result

    return _capture_baseline(
        ctx, test_command, target_ref, log=ctx.store.log_path(n),
        report=ctx.store.root / "runs" / f"issue-{n}-acceptance-baseline.xml", check=check,
        prepare=lambda: _phase(n, ctx, "checking"),
        wait=lambda acquire: _credited_wait(n, ctx, acquire),
    )


def _capture_baseline(
    ctx: RunContext, test_command: str | None, target_ref: str, *, log: Path, report: Path,
    check: Callable[[str, Path], CheckResult], prepare: Callable[[], None],
    wait: Callable[[Callable[[], object]], None], required_ids: frozenset[str] | None = None,
) -> AcceptanceBaseline | None:
    """Share capture, safety validation and cache identity across runner and premerge."""
    command = _pytest_command(test_command) if test_command else None
    if command is None:
        reason = "no test command is known" if test_command is None else f"not a supported pytest command: {test_command}"
        _append(log, f"Acceptance baseline skipped (count=0): {reason}")
        return None
    base_sha = ctx.git.ref_sha(target_ref)
    excluded = _baseline_exclusions(ctx, base_sha, log)
    cache, key = _baseline_cache(ctx, test_command, base_sha, excluded)
    cache.root.mkdir(parents=True, exist_ok=True)
    cached_ids = cache.root / f"{key}.json"

    def save_baseline(node_ids: tuple[str, ...], collected_ids: tuple[str, ...],
                      failing_ids: tuple[str, ...]) -> None:
        cache._atomic_write(cached_ids, json.dumps({
            "node_ids": node_ids, "collected_ids": collected_ids, "failing_ids": failing_ids,
            "excluded": excluded,
            "scope": sha256(json.dumps(_baseline_identity(ctx, test_command, base_sha),
                                       sort_keys=True).encode()).hexdigest(),
            "created_at": ctx.now().isoformat(),
        }) + "\n")

    with (cache.root / f"{key}.lock").open("a", encoding="utf-8") as lock:
        prepare()
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Another run builds this baseline (possibly queued for a test slot); not our run time.
            wait(lambda: fcntl.flock(lock.fileno(), fcntl.LOCK_EX))
        saved = cache.load_acceptance_baseline(key)
        if saved is not None and (required_ids is None or
                                  {"collected_ids", "failing_ids", "excluded"} <= saved.keys()):
            _append(log, f"Acceptance baseline count={len(saved['node_ids'])}, cache=hit")
            return _saved_baseline(command, saved)
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
                # Prove the selection empty from the checkout, without relying on pytest being installed.
                # Any remaining file may be an input to project-specific collectors.
                excluded_paths = set(excluded)
                if not any(path.is_file() and path.relative_to(worktree).as_posix() not in excluded_paths
                           for path in (worktree / ctx.config.acceptance_dir).rglob("*")):
                    save_baseline((), (), ())
                    _append(log, "Acceptance baseline count=0: no acceptance test files remain after exclusions")
                    return AcceptanceBaseline(command, (), (), ())
                # A compatible cache can cover a broader selection. Establish
                # the current selection before reusing its recorded outcomes.
                if required_ids is not None:
                    saved = _premerge_baseline(ctx, test_command, base_sha, excluded, required_ids)
                    if saved is not None:
                        _append(log, f"Acceptance baseline count={len(saved['node_ids'])}, cache=hit")
                        return _saved_baseline(command, saved)
                selection = f"{command} {shlex.quote(ctx.config.acceptance_dir)}"
                selection += "".join(f" {shlex.quote('--ignore=' + name)}" for name in excluded)
                baseline_command = (
                    f"{selection} "
                    f"-o junit_family=legacy --junit-prefix= --junitxml={shlex.quote(str(report.resolve()))}"
                )
                offset = log.stat().st_size if log.exists() else 0
                result = check(baseline_command, worktree)
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
                    collected_ids = tuple(sorted({junit_node_id(case) for case in junit.iter("testcase")}
                                                 - {None}))
                    failing_ids = tuple(sorted({junit_node_id(case) for case in junit.iter("testcase")
                                                if any(case.find(tag) is not None for tag in ("failure", "error"))}
                                               - {None}))
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
                        save_baseline(node_ids, collected_ids, failing_ids)
                    else:
                        _append(log, f"Acceptance baseline not cached: collected={collected}, recorded={recorded}")
                _append(log, f"Acceptance baseline count={len(node_ids)}, exit_code={result.exit_code}, timed_out={result.timed_out}")
                if not cacheable:
                    raise AttemptFailure(
                        f"acceptance baseline unavailable: exit_code={result.exit_code}, timed_out={result.timed_out}; "
                        "capture did not produce a complete, usable JUnit report"
                    )
                return AcceptanceBaseline(command, node_ids, collected_ids, failing_ids)
            finally:
                ctx.git.remove_worktree(worktree)
