"""Capture and cache the passing acceptance set on the selected base."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
import json
from math import isfinite
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from time import monotonic
from typing import IO, TYPE_CHECKING
import xml.etree.ElementTree as ET

from foragent.records import load_record
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
    # Locked files this baseline did not run because open issues own them (#376).
    excluded: tuple[str, ...] = ()
    # The base commit this set was measured on; None when unknown.
    base_sha: str | None = None


@dataclass(frozen=True)
class BaselineBuild:
    """Freeze the cache identity before a background build queues for a slot."""

    base_sha: str
    test_command: str
    excluded: list[str]
    key: str
    scope: str


def _validate_baseline_checkout(git, base_sha: str) -> None:
    """A measurement must describe the requested base, including its index."""
    if (git.head_sha() != base_sha
            or git._call(["status", "--porcelain=v1", "--untracked-files=no"])):
        raise AttemptFailure("acceptance baseline unavailable: capture modified tracked files")


def _baseline_build(ctx: RunContext, test_command: str, target_ref: str, log: Path) -> BaselineBuild:
    base_sha = ctx.git.ref_sha(target_ref)
    excluded = _baseline_exclusions(ctx, base_sha, log)
    _, key = _baseline_cache(ctx, test_command, base_sha, excluded)
    scope = sha256(json.dumps(_baseline_identity(ctx, test_command, base_sha), sort_keys=True).encode()).hexdigest()
    return BaselineBuild(base_sha, test_command, excluded, key, scope)


def _build_baseline(ctx: RunContext, build: BaselineBuild, lock: IO[str]) -> None:
    from foragent import capacity, runner

    cache = StateStore(ctx.store.root.parent / "baselines", now=ctx.now)
    log = cache.root / f"{build.key}.log"
    with lock:
        try:
            _capture_baseline(
                ctx, build.test_command, build.base_sha, log=log,
                report=cache.root / f"{build.key}.xml",
                check=lambda command, root: runner._check(
                    command, root, log, _baseline_capture_limit(ctx, log),
                    slot=capacity.hold_test_slot(ctx.config.test_slots)),
                prepare=lambda: None, wait=lambda acquire: acquire(), build=build, lock=lock,
            )
        except Exception as exc:
            # No failure marker is cached: a later tick can reserve the key again.
            _append(log, f"Acceptance baseline build failed for {build.base_sha}: {exc}")


def start_baseline_thread(ctx: RunContext, build: BaselineBuild, lock: IO[str]) -> None:
    """Background launcher for callers that own a persistent Python process."""
    threading.Thread(target=_build_baseline, args=(ctx, build, lock), daemon=True).start()


def start_baseline_process(ctx: RunContext, build: BaselineBuild, lock: IO[str]) -> None:
    """The short-lived gate CLI hands its reservation to an independent process."""
    from dataclasses import asdict
    from foragent import capacity

    request = {
        "repo_root": str(ctx.repo_root.resolve()), "store_root": str(ctx.store.root.resolve()),
        "acceptance_dir": ctx.config.acceptance_dir, "test_slots": ctx.config.test_slots,
        "machine_root": str(capacity.machine_root().resolve()),
        "build": asdict(build),
    }
    log = ctx.store.root.parent / "baselines" / f"{build.key}.log"
    with log.open("ab") as stream:
        subprocess.Popen(
            [sys.executable, "-m", "foragent.run_baseline", json.dumps(request), str(lock.fileno())],
            pass_fds=(lock.fileno(),), stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
            start_new_session=True,
            env={name: value for name, value in os.environ.items() if name != capacity.TEST_SLOT_ENV},
        )
    lock.close()


def prebuild_acceptance_baseline(ctx: RunContext, target_ref: str, launch: Callable) -> None:
    """Reserve one cold key and start it without waiting for either tests or slots."""
    from foragent import runner
    from foragent.orch_events import append_event

    if ctx.config.checks == "ci":
        return
    test_command = ctx.config.test_command or next(iter(runner.detect_test_commands(ctx.repo_root)), None)
    if test_command is None or _pytest_command(test_command) is None:
        return
    log = ctx.store.root / "baseline-build.log"
    build = _baseline_build(ctx, test_command, target_ref, log)
    cache = StateStore(ctx.store.root.parent / "baselines", now=ctx.now)
    if cache.load_acceptance_baseline(build.key) is not None:
        return
    lock = cache.lock_acceptance_baseline(build.key, blocking=False)
    if lock is None:
        return
    try:
        # A producer may have published between our initial read and reservation.
        if cache.load_acceptance_baseline(build.key) is not None:
            lock.close()
            return
        append_event(ctx.store, ctx.now(), None, "baseline-build",
                     f"Building acceptance baseline for {build.base_sha} (key={build.key})")
        launch(ctx, build, lock)
    except Exception as exc:
        lock.close()
        _append(log, f"Acceptance baseline launch failed for {build.base_sha}: {exc}")


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


def unmerged_acceptance_owners(ctx: RunContext) -> dict[str, set[int]]:
    """Locked file -> approved issues that own it and have not merged (#327)."""
    merged = {run.number for run in ctx.store.read_history() if run.outcome == "merged"}
    owners: dict[str, set[int]] = {}
    for path in (ctx.store.root / "approvals").glob("*.json"):
        approval = ctx.store.load_approval(int(path.stem))
        if approval is None or approval.number in merged:
            continue
        for name in approval.acceptance_blobs:
            owners.setdefault(name, set()).add(approval.number)
    return owners


def _baseline_exclusions(ctx: RunContext, base_sha: str, log: Path) -> list[str]:
    """Locked files owned by approved issues that have not merged (#327)."""
    owners = unmerged_acceptance_owners(ctx)
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
                              tuple(saved["failing_ids"]) if "failing_ids" in saved else None,
                              tuple(saved.get("excluded", ())))


def _baseline_capture_limit(ctx: RunContext, log: Path, remaining: int | None = None) -> int:
    """Use this repository's latest successful capture, even after its cache expires."""
    latest = None
    for path in (ctx.store.root.parent / "baselines").glob("*.json"):
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                continue
            report = path.with_suffix(".xml")
            if "repository" in saved:
                if saved["repository"] != str(ctx.repo_root.resolve()):
                    continue
            elif not report.is_file():
                # Legacy entries lack repository tags; accept companion reports in this shared cache.
                continue
            if "duration_seconds" in saved:
                duration = saved["duration_seconds"]
            else:
                junit = ET.parse(report).getroot()
                suites = [junit] if junit.tag == "testsuite" else junit.findall("testsuite")
                if junit.tag not in {"testsuite", "testsuites"} or not suites:
                    continue
                duration = sum(float(suite.attrib["time"]) for suite in suites)
            created_at = datetime.fromisoformat(saved["created_at"])
            if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                    or not isfinite(duration) or duration < 0 or created_at.tzinfo is None
                    or created_at > ctx.now()):
                continue
            if latest is None or created_at > latest[0]:
                latest = created_at, duration
        except (OSError, ValueError, TypeError, KeyError, ET.ParseError):
            continue
    limit = 900 if latest is None else int(min(3600, max(900, 2 * latest[1])))
    reason = "default: no successful capture history" if latest is None else f"last successful capture={latest[1]:g}s"
    if remaining is not None:
        limit = min(limit, remaining)
        reason += f", run remaining={remaining}s"
    _append(log, f"Acceptance baseline capture starting: limit={limit}s ({reason})")
    return limit


def _capture_acceptance_baseline(
    n: int, ctx: RunContext, test_command: str | None, target_ref: str,
) -> AcceptanceBaseline | None:
    from foragent import runner

    def check(command: str, worktree: Path) -> CheckResult:
        remaining = runner._remaining(n, ctx)
        if remaining <= 0:
            raise AttemptFailure("acceptance baseline unavailable: run budget exhausted before capture")
        result = runner._check(command, worktree, ctx.store.log_path(n),
                               _baseline_capture_limit(ctx, ctx.store.log_path(n), remaining),
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
    build: BaselineBuild | None = None, lock: IO[str] | None = None,
) -> AcceptanceBaseline | None:
    """Share capture, safety validation and cache identity across runner and premerge."""
    command = _pytest_command(test_command) if test_command else None
    if command is None:
        reason = "no test command is known" if test_command is None else f"not a supported pytest command: {test_command}"
        _append(log, f"Acceptance baseline skipped (count=0): {reason}")
        return None
    build = build or _baseline_build(ctx, test_command, target_ref, log)
    base_sha, excluded, key = build.base_sha, build.excluded, build.key
    cache = StateStore(ctx.store.root.parent / "baselines", now=ctx.now)
    cached_ids = cache.root / f"{key}.json"

    def save_baseline(node_ids: tuple[str, ...], collected_ids: tuple[str, ...],
                      failing_ids: tuple[str, ...], duration_seconds: float | None = None) -> None:
        saved = {
            "node_ids": node_ids, "collected_ids": collected_ids, "failing_ids": failing_ids,
            "excluded": excluded,
            "scope": build.scope,
            "created_at": ctx.now().isoformat(),
        }
        if duration_seconds is not None:
            saved.update(repository=str(ctx.repo_root.resolve()), duration_seconds=duration_seconds)
        cache._atomic_write(cached_ids, json.dumps(saved) + "\n")

    prepare()
    if lock is None:
        lock = cache.lock_acceptance_baseline(key, blocking=False)
        if lock is None:
            # Another run builds this baseline (possibly queued for a test slot); not our run time.
            def acquire():
                nonlocal lock
                lock = cache.lock_acceptance_baseline(key)
            wait(acquire)
        ownership = lock
    else:
        ownership = nullcontext(lock)
    with ownership:
        saved = cache.load_acceptance_baseline(key)
        if saved is not None and (required_ids is None or
                                  {"collected_ids", "failing_ids", "excluded"} <= saved.keys()):
            _append(log, f"Acceptance baseline count={len(saved['node_ids'])}, cache=hit")
            return replace(_saved_baseline(command, saved), excluded=tuple(excluded), base_sha=base_sha)
        report.parent.mkdir(parents=True, exist_ok=True)
        # Use a fresh base checkout; tests can write files without touching the implementation.
        with tempfile.TemporaryDirectory(prefix="foragent-baseline-") as directory:
            worktree = Path(directory) / "worktree"
            capture_report = Path(directory) / "capture.xml"
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
                    return AcceptanceBaseline(command, (), (), (), tuple(excluded), base_sha)
                # A compatible cache can cover a broader selection. Establish
                # the current selection before reusing its recorded outcomes.
                if required_ids is not None:
                    saved = _premerge_baseline(ctx, test_command, base_sha, excluded, required_ids)
                    if saved is not None:
                        _append(log, f"Acceptance baseline count={len(saved['node_ids'])}, cache=hit")
                        return replace(_saved_baseline(command, saved), excluded=tuple(excluded), base_sha=base_sha)
                selection = f"{command} {shlex.quote(ctx.config.acceptance_dir)}"
                selection += "".join(f" {shlex.quote('--ignore=' + name)}" for name in excluded)
                baseline_command = (
                    f"{selection} "
                    f"-o junit_family=legacy --junit-prefix= --junitxml={shlex.quote(str(capture_report))}"
                )
                offset = log.stat().st_size if log.exists() else 0
                started = monotonic()
                result = check(baseline_command, worktree)
                duration_seconds = monotonic() - started
                _validate_baseline_checkout(ctx.git.at(worktree), base_sha)
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
                    node_ids = _passing_acceptance_ids(capture_report)
                    junit = ET.parse(capture_report)
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
                    if not cacheable:
                        _append(log, f"Acceptance baseline not cached: collected={collected}, recorded={recorded}")
                # A failed refresh must not replace the last successful legacy XML history.
                # Validate only the fresh report so a missing result cannot reuse that history as evidence.
                if cacheable or not cached_ids.is_file():
                    if capture_report.is_file():
                        shutil.copyfile(capture_report, report)
                    else:
                        report.unlink(missing_ok=True)
                if cacheable:
                    save_baseline(node_ids, collected_ids, failing_ids, duration_seconds)
                _append(log, f"Acceptance baseline count={len(node_ids)}, exit_code={result.exit_code}, timed_out={result.timed_out}")
                if not cacheable:
                    raise AttemptFailure(
                        f"acceptance baseline unavailable: exit_code={result.exit_code}, timed_out={result.timed_out}; "
                        "capture did not produce a complete, usable JUnit report"
                    )
                return AcceptanceBaseline(command, node_ids, collected_ids, failing_ids, tuple(excluded), base_sha)
            finally:
                ctx.git.remove_worktree(worktree)


def surviving_baseline(baseline: AcceptanceBaseline, git, worktree: Path, acceptance_dir: str,
                       check: Callable[[str, Path], CheckResult], log: Path) -> AcceptanceBaseline:
    """Drop baseline node ids that the current tree no longer collects (#123).

    A base sync can rename or delete a locked test. Selecting its old id makes
    pytest stop with "not found" and hides the tests that still exist. Collect
    the same selection the capture ran, but only when the staged acceptance
    files differ from the measured base. When collection itself fails, keep
    every id so the verification still reports that failure.
    """
    if not baseline.node_ids or baseline.base_sha is None:
        return baseline
    # Only a tree whose acceptance files differ from the measured base can lose ids.
    if not git._call(["diff", "--cached", "--name-only", baseline.base_sha, "--", acceptance_dir]):
        return baseline
    if not (worktree / acceptance_dir).is_dir():
        collected: set[str] | None = set()
    else:
        selection = f"{baseline.command} {shlex.quote(acceptance_dir)}"
        selection += "".join(f" {shlex.quote('--ignore=' + name)}" for name in baseline.excluded)
        offset = log.stat().st_size if log.exists() else 0
        result = check(f"{selection} --collect-only --verbosity=-1", worktree)
        collected = None
        if not result.timed_out and result.exit_code in (0, 5):
            with log.open(encoding="utf-8", errors="replace") as stream:
                stream.seek(offset)
                collected = {line.strip() for line in stream if "::" in line}
        else:
            _append(log, "Acceptance baseline collection failed; keeping every baseline node id")
    if collected is None:
        return baseline
    missing = [node for node in baseline.node_ids if node not in collected]
    for node in missing:
        _append(log, f"Acceptance baseline dropped missing node id: {node}")
    if not missing:
        return baseline
    kept = tuple(node for node in baseline.node_ids if node in collected)
    _append(log, f"Acceptance baseline dropped {len(missing)} missing node ids; {len(kept)} remain")
    return replace(baseline, node_ids=kept)


if __name__ == "__main__":
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from foragent.config import Config
    from foragent.git import GitRepo
    from foragent import capacity

    request = json.loads(sys.argv[1])
    capacity.machine_root = lambda: Path(request["machine_root"])
    root = Path(request["repo_root"])
    context = SimpleNamespace(
        repo_root=root, store=StateStore(Path(request["store_root"])), git=GitRepo(root),
        config=Config(acceptance_dir=request["acceptance_dir"], test_slots=request["test_slots"]),
        now=lambda: datetime.now(UTC),
    )
    _build_baseline(context, load_record(BaselineBuild, request["build"]), os.fdopen(int(sys.argv[2]), "a"))
