"""Independent checks, pytest selection and verification of implementation contents."""

from __future__ import annotations

import ast
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import TYPE_CHECKING
import xml.etree.ElementTree as ET

import yaml

from foragent import capacity
from foragent.actions import flag, invalidate_approval
from foragent.config import Config
from foragent.git import GitRepo
from foragent.issue import ParsedIssue, has_machine_check, parse_body
from foragent.reasons import Reason
from foragent.records import load_record
from foragent.run_attempt import AttemptFailure, ExecutorStopped
from foragent.run_execution import _claude_run_dir
from foragent.run_waits import _phase, _progress, _test_slot, _timeout
from foragent.state import Approval

if TYPE_CHECKING:
    from foragent.runner import RunContext
    from foragent.run_attempt import Implementation


@dataclass(frozen=True)
class CheckResult:
    command: str
    exit_code: int | None
    timed_out: bool
    output: str


def _append(log: Path, text: str) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as stream:
        stream.write(text + "\n")


def _check(
    command: str, worktree: Path, log: Path, timeout_s: int,
    *, progress: Callable[[], None] | None = None,
    slot: AbstractContextManager | None = None,
) -> CheckResult:
    from foragent import runner

    _append(log, f"\nDone when command:\n{command}")
    offset = log.stat().st_size

    # Checks run in the implementation worktree, whose changes are staged and
    # published afterwards. Bytecode written by the checks must not join them.
    environment = {"PYTHONDONTWRITEBYTECODE": "1"}

    def execute():
        # The slot covers only the command; timeout_s counts execution, not the queue.
        if slot is None:
            return runner.run_logged(["/bin/sh", "-c", command], cwd=worktree, timeout_s=timeout_s, log_path=log,
                                     env=environment)
        with slot:
            # A nested `fa test` in the command shares this slot instead of waiting for another.
            return runner.run_logged(["/bin/sh", "-c", command], cwd=worktree, timeout_s=timeout_s, log_path=log,
                                     env={**environment, **capacity.HELD_ENV})

    # A quiet check is still running; keep the scheduler informed while it waits.
    code, timed_out, _ = runner.poll_executor(execute, progress) if progress is not None else execute()
    with log.open("rb") as stream:
        stream.seek(max(offset, log.stat().st_size - 16000))
        tail = stream.read().decode("utf-8", errors="replace")[-4000:]
    _append(log, f"\nCheck exit_code={code}, timed_out={timed_out}\nOutput tail:\n{tail}")
    return CheckResult(command, code, timed_out, tail)


def _pytest_command(command: str) -> str | None:
    """Keep the pytest invocation/options, replacing its test selection later."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return None
    index = next((i for i, word in enumerate(words)
                  if Path(word).name == "pytest"), None)
    if index is None or any(word and all(char in "();<>|&" for char in word) for word in words):
        return None
    # A full-suite command can exclude acceptance tests or select other paths.
    # Neither that selection nor early stopping should narrow the baseline.
    # Default terminal verbosity exposes this execution's collection count.
    selection_values = {"--ignore", "--ignore-glob", "--deselect", "-k", "-m", "--maxfail", "--junitxml", "--junit-xml",
                        "--verbosity", "--color"}
    selection_flags = {"-x", "--exitfirst", "--lf", "--last-failed", "--ff", "--failed-first",
                       "--sw", "--stepwise", "--sw-skip", "--stepwise-skip", "--collect-only", "--co",
                       "--quiet", "--verbose"}
    option_values = {"-c", "--config-file", "-o", "--override-ini", "-p", "--rootdir", "--confcutdir",
                     "--basetemp", "--import-mode", "--tb", "--capture", "--color", "--code-highlight",
                     "--junit-prefix", "--verbosity", "--durations", "--durations-min"}
    result = words[:index + 1]
    args = iter(words[index + 1:])
    for arg in args:
        option = arg.split("=", 1)[0]
        if option in selection_values:
            if "=" not in arg:
                next(args, None)
        elif arg in selection_flags or arg == "--" or re.fullmatch(r"-[qvx]+", arg):
            continue
        elif option in option_values and "=" not in arg:
            result.append(arg)
            if (value := next(args, None)) is not None:
                result.append(value)
        elif arg.startswith("-"):
            result.append(arg)
        # Remaining positional arguments are test paths/node ids.
    # Preserve shell environment assignments before the executable.
    prefix = []
    while result and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*=.*", result[0], re.S):
        name, value = result.pop(0).split("=", 1)
        prefix.append(f"{name}={shlex.quote(value)}")
    # Inherited or inline addopts must not reintroduce selection/early stopping.
    prefix.append("PYTEST_ADDOPTS=")
    # Stable, repository-relative JUnit paths are also valid pytest selectors.
    return " ".join([*prefix, shlex.join(result), "-o addopts= --rootdir=. --maxfail=0 -p no:cacheprovider"])


def _pytest_invocation(command: str | None) -> str | None:
    """The shell prefix that starts pytest (environment, launcher, executable), without its options."""
    if command is None:
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return None
    index = next((i for i, word in enumerate(words) if Path(word).name == "pytest"), None)
    if index is None or any(word and all(char in "();<>|&" for char in word) for word in words):
        return None
    words = words[:index + 1]
    prefix = []
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*=.*", words[0], re.S):
        name, value = words.pop(0).split("=", 1)
        prefix.append(f"{name}={shlex.quote(value)}")
    return " ".join([*prefix, shlex.join(words)])


def run_tests(root: Path, config: Config, args: list[str]) -> int:
    """`fa test`: wait for a machine-wide test slot, then run pytest with these arguments."""
    from foragent import runner

    command = config.test_command or next(iter(runner.detect_test_commands(root)), None)
    invocation = _pytest_invocation(command) or "pytest"
    announced = False

    def waiting() -> None:
        nonlocal announced
        if not announced:
            announced = True
            print("foragent: waiting for a machine-wide test slot", file=sys.stderr, flush=True)

    with capacity.hold_test_slot(config.test_slots, waiting=waiting):
        # A test that itself runs `fa test` shares this slot instead of waiting for another.
        return subprocess.run(["/bin/sh", "-c", f"{invocation} {shlex.join(args)}".rstrip()],
                              cwd=root, check=False, env={**os.environ, **capacity.HELD_ENV}).returncode


def junit_node_id(case: ET.Element) -> str | None:
    """Legacy JUnit provides exact file paths; classname adds nested classes."""
    file, name = case.get("file"), case.get("name")
    if not file or not name:
        return None
    module = file.removesuffix(".py").replace("/", ".")
    classname = case.get("classname", "")
    classes = classname.removeprefix(module).lstrip(".").replace(".", "::")
    return "::".join(part for part in (file, classes, name) if part)


def _passing_acceptance_ids(report: Path) -> tuple[str, ...]:
    passed, failed = set(), set()
    for case in ET.parse(report).iter("testcase"):
        if (node_id := junit_node_id(case)) is None:
            continue
        (failed if any(case.find(tag) is not None for tag in ("failure", "error", "skipped")) else passed).add(node_id)
    return tuple(sorted(passed - failed))


def has_pull_request_ci(root: Path) -> bool:
    """Whether a GitHub Actions workflow in this checkout runs on pull_request (read from files)."""
    workflows = root / ".github" / "workflows"
    if not workflows.is_dir():
        return False
    for path in sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")]):
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            continue
        if not isinstance(document, dict):
            continue
        # YAML 1.1 reads the bare key `on` as the boolean true.
        events = document.get("on", document.get(True))
        if isinstance(events, str):
            events = [events]
        if isinstance(events, (list, dict)) and "pull_request" in events:
            return True
    return False


def trusts_ci(root: Path, config: Config) -> bool:
    """D81: leave importer-only unit tests to pull_request CI (`runner_unit_tests`)."""
    if config.runner_unit_tests == "auto":
        return has_pull_request_ci(root)
    return config.runner_unit_tests == "ci"


def _related_unit_tests(
    worktree: Path, paths: list[str], acceptance_dir: str, *, own_only: bool = False,
) -> tuple[str, ...]:
    """Select changed pytest files and direct static importers of changed modules.

    With `own_only` (CI runs the full suite), changed modules contribute only their own
    test files (`test_<name>.py` or `<name>_test.py`) instead of every importer.
    """
    acceptance = Path(acceptance_dir)
    tests = {
        path.relative_to(worktree).as_posix(): path
        for path in (worktree / "tests").rglob("*.py")
        if (path.name.startswith("test_") or path.name.endswith("_test.py"))
        and not path.relative_to(worktree).is_relative_to(acceptance)
    }
    selected = set(paths) & tests.keys()
    modules = set()
    for name in paths:
        path = Path(name)
        if path.suffix != ".py" or path.is_relative_to("tests") or path.is_relative_to(acceptance):
            continue
        parts = list(path.with_suffix("").parts)
        if parts[0] == "src":
            parts.pop(0)
        if parts and parts[-1] == "__init__":
            parts.pop()
        if parts:
            modules.add(".".join(parts))
    if not modules:
        return tuple(sorted(selected))
    if own_only:
        stems = {module.rsplit(".", 1)[-1] for module in modules}
        own = {f"test_{stem}.py" for stem in stems} | {f"{stem}_test.py" for stem in stems}
        return tuple(sorted(selected | {name for name, path in tests.items() if path.name in own}))
    for name, path in tests.items():
        if name in selected:
            continue
        try:
            tree = ast.parse(path.read_bytes(), filename=name)
        except (SyntaxError, UnicodeError):
            continue
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                imports.add(node.module)
                imports.update(f"{node.module}.{alias.name}" for alias in node.names)
        if any(imported == module or imported.startswith(module + ".")
               for imported in imports for module in modules):
            selected.add(name)
    return tuple(sorted(selected))


def check_done(snapshot: Path, worktree: Path, acceptance_dir: str) -> int:
    """Stop hook: re-use independent checks, allowing at most eight blocked stops."""
    from foragent import runner

    directory = _claude_run_dir(snapshot)
    directory.mkdir(parents=True, exist_ok=True)
    counter = directory / "stop-block-count"
    blocked = 0
    try:
        blocked = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
        if blocked >= 8:
            print("Done when: Stop hook block cap reached; runner verification will decide.")
            return 0
        approval = load_record(Approval, json.loads(snapshot.read_text(encoding="utf-8")))
        parsed = parse_body(approval.body)
        if not has_machine_check(parsed):
            raise ValueError("Done when has no machine check")
        for command in parsed.done_when_commands:
            result = runner._check(command, worktree, directory / "check-done.log", 900)
            if result.exit_code != 0 or result.timed_out:
                counter.write_text(str(blocked + 1), encoding="utf-8")
                print(f"Done when failed: {command}\n{result.output[-2000:]}"
                      + ("\nCheck timed out." if result.timed_out else ""), file=sys.stderr)
                return 2
    except (OSError, ValueError, TypeError) as exc:
        # Malformed snapshots and check-launch failures must also block a stop.
        counter.write_text(str(blocked + 1), encoding="utf-8")
        print(f"Done when failed: {exc}", file=sys.stderr)
        return 2
    print(f"Done when: all {len(parsed.done_when_commands)} commands passed; acceptance lock: {acceptance_dir}.")
    return 0


def _acceptance_matches(
    n: int, ctx: RunContext, git: GitRepo, approval: Approval, ref: str = "HEAD",
) -> bool:
    blobs = git.blob_shas(ref, approval.acceptance_blobs)
    mismatches = [path for path, sha in approval.acceptance_blobs.items() if blobs.get(path) != sha]
    if mismatches:
        invalidate_approval(
            ctx, n, "Approval invalidated: worktree acceptance files changed. Re-approve to run it.",
            "\n".join(f"Acceptance file changed: `{path}`." for path in mismatches),
        )
    return not mismatches


def _verify_implementation(attempt: Implementation) -> str:
    from foragent import runner
    from foragent.run_conflicts import _prepare_target
    from foragent.run_publish import _check_publication
    from foragent.ci_mode import enabled
    from foragent.ci_local import verify_local_only_units

    n, ctx, git = attempt.n, attempt.ctx, attempt.git
    worktree, log = attempt.worktree, attempt.log
    _phase(n, ctx, "checking")
    git.stage_changes()
    if not _prepare_target(attempt):
        raise ExecutorStopped("hash-mismatch")
    attempt.paths = git.changed_paths(attempt.start_ref)
    _progress(n, ctx)
    attempt.checks = []
    # Cleaning tracked runner scratch files is not an implementation change.
    if not any(not path.startswith(".foragent/") for path in attempt.paths):
        return "The session made no changes."
    staged_tree = git.staged_tree_sha()
    for verification in range(2):
        attempt.checks = []
        for command in attempt.parsed.done_when_commands:
            attempt.checks.append(runner._check(command, worktree, log, 900, progress=lambda: _progress(n, ctx),
                                 slot=_test_slot(n, ctx)))
            _progress(n, ctx)
        if attempt.baseline and attempt.baseline.node_ids:
            from foragent.run_baseline import surviving_baseline

            attempt.baseline = surviving_baseline(
                attempt.baseline, git, worktree,
                lambda command, root: runner._check(command, root, log, 900, progress=lambda: _progress(n, ctx),
                                                    slot=_test_slot(n, ctx)), log,
                f"origin/{attempt.target_branch}", scratch=ctx.store.root / "runs")
            if not attempt.baseline.node_ids:
                _append(log, "Acceptance baseline verification skipped (count=0): no baseline node ids remain")
        if attempt.baseline and attempt.baseline.node_ids:
            started = ctx.monotonic()
            command = f"{attempt.baseline.command} {shlex.join(attempt.baseline.node_ids)}"
            result = runner._check(command, worktree, log, 900, progress=lambda: _progress(n, ctx),
                            slot=_test_slot(n, ctx))
            attempt.checks.append(result)
            _append(log, f"Acceptance baseline verification count={len(attempt.baseline.node_ids)}, "
                    f"exit_code={result.exit_code}, timed_out={result.timed_out}")
            _progress(n, ctx)
            if not enabled(ctx, attempt.target_branch):
                previous = ctx.store.get_run(n).comparison or {}
                ctx.store.update_run(n, comparison={"source": "local", "duration_seconds":
                    previous.get("duration_seconds", 0) + max(0, ctx.monotonic() - started)})
        test_command = ctx.config.test_command
        if test_command is None:
            test_command = next(iter(runner.detect_test_commands(worktree)), None)
        unit_command = _pytest_command(test_command) if test_command else None
        unit_tests = _related_unit_tests(worktree, attempt.paths, ctx.config.acceptance_dir,
                                         own_only=ctx.config.runner_unit_tests != "all"
                                         and (enabled(ctx, attempt.target_branch) or trusts_ci(worktree, ctx.config)))
        if unit_command:
            local_units = verify_local_only_units(attempt, unit_command)
            if local_units is not None:
                attempt.checks.append(local_units)
        if unit_tests and unit_command:
            limit = max(900, runner._remaining(n, ctx))
            result = runner._check(f"{unit_command} {shlex.join(unit_tests)}", worktree, log, limit,
                            progress=lambda: _progress(n, ctx), slot=_test_slot(n, ctx))
            attempt.checks.append(result)
            _progress(n, ctx)
            if result.timed_out:
                detail = f"Related unit tests timed out after {limit} s ({len(unit_tests)} selected tests)."
                flag(ctx, n, Reason.TIMEOUT, detail)
                raise ExecutorStopped("timeout", detail)
        elif unit_tests:
            _append(log, "Related unit tests skipped: no supported pytest command is known")
        git.stage_changes()
        if not _prepare_target(attempt):
            raise ExecutorStopped("hash-mismatch")
        attempt.paths = git.changed_paths(attempt.start_ref)
        checked_tree = git.staged_tree_sha()
        if checked_tree != staged_tree:
            if verification == 1:
                return "Done when commands modify tracked files"
            _append(log, "Done when changed staged contents; checking the final contents again.")
            staged_tree = checked_tree
            continue
        failures = [check for check in attempt.checks if check.exit_code != 0 or check.timed_out]
        if failures:
            check = failures[-1]
            return f"Command: {check.command}\n{check.output}" + ("\nCheck timed out." if check.timed_out else "")
        try:
            _check_publication(git, attempt.target_branch)
        except AttemptFailure as exc:
            return str(exc)
        return "" if any(not path.startswith(".foragent/") for path in attempt.paths) else "The session made no changes."
    raise AssertionError("unreachable verification state")


def _verify_review_head(
    n: int, ctx: RunContext, git: GitRepo, parsed: ParsedIssue, head: str, link: str,
) -> str | None:
    from foragent import runner

    worktree = git.root
    log = ctx.store.log_path(n)
    for command in parsed.done_when_commands:
        if runner._remaining(n, ctx) <= 0:
            return _timeout(n, ctx)
        check = runner._check(command, worktree, log, min(900, runner._remaining(n, ctx)), progress=lambda: _progress(n, ctx),
                       slot=_test_slot(n, ctx))
        _progress(n, ctx)
        if check.timed_out or runner._remaining(n, ctx) <= 0:
            return _timeout(n, ctx)
        if check.exit_code != 0:
            flag(ctx, n, Reason.RETRIES_EXCEEDED, "Done when failed on the PR head.",
                 f"Command: {command}\n{check.output}\n{link}")
            return "checks-failed"
    git.stage_changes()
    if git.head_sha() != head or git.has_staged_changes():
        flag(ctx, n, Reason.RETRIES_EXCEEDED, "Done when changed the verified PR contents.", link)
        return "checks-failed"
    return None
