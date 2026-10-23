"""Test commands, acceptance baselines and Done when checks."""

from datetime import datetime, timedelta
from hashlib import sha256
import ast
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from foragent import capacity
from foragent.config import Config
from foragent.issue import has_machine_check, parse_body
from foragent.order import detect_test_commands
from foragent.state import Approval, StateStore
from foragent.runner_core import AcceptanceBaseline, ExecutorStopped, RunContext, _append, _check, _credited_wait, _phase, _progress, _remaining, _test_slot, _timeout


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
    command = config.test_command or next(iter(detect_test_commands(root)), None)
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


def _related_unit_tests(worktree: Path, paths: list[str], acceptance_dir: str) -> tuple[str, ...]:
    """Select changed pytest files and direct static importers of changed modules."""
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
    log = ctx.store.log_path(n)
    command = _pytest_command(test_command) if test_command else None
    if command is None:
        reason = "no test command is known" if test_command is None else f"not a supported pytest command: {test_command}"
        _append(log, f"Acceptance baseline skipped (count=0): {reason}")
        return None
    base_sha = ctx.git.ref_sha(target_ref)
    key = sha256(json.dumps(
        [base_sha, test_command, ctx.config.acceptance_dir, _baseline_environment(ctx.repo_root)],
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
                if _remaining(n, ctx) <= 0:
                    raise ExecutorStopped(_timeout(n, ctx))
                selection = f"{command} {shlex.quote(ctx.config.acceptance_dir)}"
                baseline_command = (
                    f"{selection} "
                    f"-o junit_family=legacy --junit-prefix= --junitxml={shlex.quote(str(report.resolve()))}"
                )
                offset = log.stat().st_size if log.exists() else 0
                result = _check(baseline_command, worktree, log, min(900, _remaining(n, ctx)),
                                progress=lambda: _progress(n, ctx), slot=_test_slot(n, ctx))
                _progress(n, ctx)
                # Read this execution's collection count from the full log: a long
                # traceback can push it out of CheckResult's bounded output tail.
                collected = None
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
                    # --continue-on-collection-errors can make collection failures exit 1.
                    collection_errors = junit.find(".//error[@message='collection failure']") is not None
                except (OSError, ET.ParseError) as exc:
                    _append(log, f"Acceptance baseline count=0: no usable JUnit report ({exc})")
                    node_ids = ()
                else:
                    # JUnit and the exit status are the subprocess contract;
                    # terminal collection output is an optional cross-check.
                    if (not result.timed_out and result.exit_code == expected_exit and not collection_errors
                            and (collected is None or collected == recorded)):
                        cache._atomic_write(cached_ids, json.dumps({
                            "node_ids": node_ids, "created_at": ctx.now().isoformat(),
                        }) + "\n")
                    else:
                        _append(log, f"Acceptance baseline not cached: collected={collected}, recorded={recorded}")
                _append(log, f"Acceptance baseline count={len(node_ids)}, exit_code={result.exit_code}, timed_out={result.timed_out}")
                return AcceptanceBaseline(command, node_ids)
            finally:
                ctx.git.remove_worktree(worktree)


def _claude_run_dir(snapshot: Path) -> Path:
    return snapshot.parent.parent / "runs" / f"issue-{snapshot.stem}"


def _write_claude_settings(n: int, ctx: RunContext, worktree: Path) -> Path:
    snapshot = (ctx.store.root / "approvals" / f"{n}.json").resolve()
    directory = _claude_run_dir(snapshot)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "stop-block-count").write_text("0", encoding="utf-8")
    acceptance = Path(os.path.relpath(worktree / ctx.config.acceptance_dir, worktree)).as_posix().rstrip("/") + "/"
    command = shlex.join([
        sys.executable, "-m", "foragent.cli", "gate", "check-done", "--snapshot", str(snapshot),
        "--worktree", str(worktree.resolve()), "--acceptance-dir", ctx.config.acceptance_dir,
    ])
    settings = {
        "permissions": {"deny": [
            f"Edit({acceptance}**)", f"Write({acceptance}**)",
            "Bash(git commit:*)", "Bash(git push:*)", "Bash(git checkout:*)", "Bash(git switch:*)",
            "Bash(git merge:*)",
        ]},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": command,
                                      "timeout": max(900, ctx.config.max_runtime_minutes * 60)}]}]},
    }
    path = directory / "claude-settings.json"
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def check_done(snapshot: Path, worktree: Path, acceptance_dir: str) -> int:
    """Stop hook: re-use independent checks, allowing at most eight blocked stops."""
    directory = _claude_run_dir(snapshot)
    directory.mkdir(parents=True, exist_ok=True)
    counter = directory / "stop-block-count"
    blocked = 0
    try:
        blocked = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
        if blocked >= 8:
            print("Done when: Stop hook block cap reached; runner verification will decide.")
            return 0
        approval = Approval(**json.loads(snapshot.read_text(encoding="utf-8")))
        parsed = parse_body(approval.body)
        if not has_machine_check(parsed):
            raise ValueError("Done when has no machine check")
        for command in parsed.done_when_commands:
            result = _check(command, worktree, directory / "check-done.log", 900)
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
