"""Entry point for repository adoption commands."""

import argparse
from collections import Counter
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import tempfile

import yaml

from foragent.config import Config, ConfigError, load_config
from foragent.git import GitError, GitRepo
from foragent.processes import run_logged


def _scaffold(path: str, acceptance_dir: str) -> bool:
    return (
        ("/" not in path and path.startswith(("README", "LICENSE")))
        or path in {".gitignore", ".foragent.yml", f"{acceptance_dir}/.gitkeep"}
        or path.startswith(".github/ISSUE_TEMPLATE/")
    )


def _hot_paths(git: GitRepo) -> list[dict]:
    # NUL-delimited commit headers and filenames preserve unusual Git paths.
    history = git._call(["log", "--format=%x00%H", "--name-only", "--no-renames", "-z"])
    counts: Counter[str] = Counter()
    for commit in history.split("\0\0"):
        paths = set()
        names = commit.lstrip("\0").split("\0")[1:]
        # Git inserts one newline between the header and the first filename.
        if names:
            names[0] = names[0].removeprefix("\n")
        for name in names:
            directories = name.split("/")[:-1]
            if directories:
                paths.add("/".join(directories[:2]))
        counts.update(paths)
    return [{"path": path, "commits": count}
            for path, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:10]]


def _test_invocations(script: str) -> list[list[str]]:
    """Find test invocations in shell commands without executing any of them."""
    lexer = shlex.shlex(script.replace("\\\n", ""), posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    commands: list[list[str]] = [[]]
    comment = False
    try:
        for token in lexer:
            if "\n" in token and all(char in ";&|()\n" for char in token):
                comment = False
            elif comment:
                continue
            elif token.startswith("#"):
                comment = True
                continue
            if all(char in ";&|()\n" for char in token):
                commands.append([])
            else:
                commands[-1].append(token)
    except ValueError:
        # A shell expression may use syntax that shlex does not understand.
        return []
    invocations = []
    for tokens in commands:
        prefixes = {"if", "then", "elif", "do", "!", "env", "command", "exec"}
        while tokens and ("=" in tokens[0] or tokens[0] in prefixes):
            tokens = tokens[1:]
        if not tokens:
            continue
        program = PurePosixPath(tokens[0]).name
        if program in {"uv", "poetry", "pipenv", "pdm", "hatch"} and "run" in tokens:
            tokens = tokens[tokens.index("run") + 1:]
            # Runner options may appear before the executable (e.g. uv run --no-sync).
            while tokens and PurePosixPath(tokens[0]).name not in {"pytest", "python", "python3", "npm", "pnpm", "go"}:
                if not tokens[0].startswith("-"):
                    break
                tokens = tokens[1:]
        if not tokens:
            continue
        program = PurePosixPath(tokens[0]).name
        if program.startswith("python") and tokens[1:3] == ["-m", "pytest"]:
            tokens = tokens[2:]
            program = "pytest"
        if program == "pytest" or (program in {"npm", "pnpm", "go"} and tokens[1:2] == ["test"]):
            invocations.append([program, *tokens[1:]])
    return invocations


def _ci(root: Path, acceptance_dir: str) -> list[dict]:
    reports = []
    for path in sorted((root / ".github/workflows").glob("*")):
        if not path.is_file() or path.suffix not in {".yml", ".yaml"}:
            continue
        try:
            workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"cannot read workflow {path}: {exc}") from exc
        jobs = workflow.get("jobs", {}) if isinstance(workflow, dict) else {}
        if not isinstance(jobs, dict):
            continue
        invocations = []
        for job in jobs.values():
            steps = job.get("steps", []) if isinstance(job, dict) else []
            if not isinstance(steps, list):
                continue
            for step in steps:
                if isinstance(step, dict) and isinstance(step.get("run"), str):
                    invocations.extend(_test_invocations(step["run"]))
        if invocations:
            reports.append({
                "file": path.relative_to(root).as_posix(),
                "runs_acceptance": any(
                    command[0] != "pytest" or f"--ignore={acceptance_dir}" not in command[1:]
                    for command in invocations
                ),
            })
    return reports


def survey(root: Path) -> dict:
    """Describe tracked history and current setup using read-only operations."""
    root = root.resolve()
    git = GitRepo(root)
    git.check_worktree()
    root = git.worktree_root()
    git = GitRepo(root)
    config = load_config(root) if (root / ".foragent.yml").is_file() else Config()
    acceptance_dir = config.acceptance_dir.rstrip("/")
    try:
        git._call(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"])
    except GitError as exc:
        if not isinstance(exc.__cause__, subprocess.CalledProcessError) or exc.__cause__.returncode != 1:
            raise
        has_commits = False
    else:
        has_commits = True
    tracked = git._call(["ls-files", "--cached", "-z"]).split("\0")
    return {
        "kind": "empty" if not has_commits or all(
            _scaffold(path, acceptance_dir) for path in tracked if path
        ) else "existing",
        "hot_paths": _hot_paths(git) if has_commits else [],
        "existing_setup": [name for name in (".claude/", "CLAUDE.md", "AGENTS.md", ".github/workflows/")
                           if ((root / name).is_dir() if name.endswith("/") else (root / name).is_file())],
        "ci": _ci(root, acceptance_dir),
    }


def _detect_test_command(root: Path) -> str | None:
    """Use order.context's runner preference, including requirements-only projects."""
    if (root / "pyproject.toml").is_file() or any(path.is_file() for path in root.glob("requirements*.txt")):
        return "uv run pytest"
    package = root / "package.json"
    if package.is_file():
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
            if isinstance(data.get("scripts"), dict) and data["scripts"].get("test"):
                pnpm = (root / "pnpm-lock.yaml").is_file() or str(data.get("packageManager", "")).startswith("pnpm@")
                return "pnpm test" if pnpm else "npm test"
        except (ValueError, AttributeError):
            pass
    if (root / "go.mod").is_file():
        return "go test ./..."
    return None


def baseline(root: Path, command: str | None = None, timeout: int = 900) -> dict:
    """Run existing tests once and report their state without treating red as an error."""
    root = root.resolve()
    command = command or _detect_test_command(root)
    if command is None:
        return {"command": None, "passed": None, "skipped": "no test command found"}
    is_pytest = any(invocation[0] == "pytest" for invocation in _test_invocations(command))
    if is_pytest:
        config = load_config(root) if (root / ".foragent.yml").is_file() else Config()
        command += " " + shlex.quote(f"--ignore={config.acceptance_dir.rstrip('/')}") + " -rf"
    with tempfile.TemporaryDirectory(prefix="fa-baseline-") as directory:
        log = Path(directory) / "output.log"
        exit_code, timed_out, _ = run_logged(
            ["/bin/sh", "-c", command], cwd=root, timeout_s=timeout, log_path=log,
        )
        output = log.read_text(encoding="utf-8", errors="replace") if is_pytest else ""
    # Explicit --color=yes can leave terminal escapes around pytest's summary.
    output = re.sub(r"\x1b\[[0-9;]*m", "", output)
    failing = []
    for line in output.splitlines():
        if line.startswith("FAILED "):
            node_id = line.removeprefix("FAILED ").split(" - ", 1)[0]
            if node_id not in failing:
                failing.append(node_id)
    return {"command": command, "exit_code": exit_code,
            "passed": exit_code == 0 and not timed_out, "failing": failing}


def run(args: argparse.Namespace) -> int:
    if args.action == "baseline":
        report = baseline(args.repo_root, args.test_command, args.timeout)
        if args.json:
            print(json.dumps(report, ensure_ascii=False))
        elif report["command"] is None:
            print(f"Baseline skipped: {report['skipped']}")
        else:
            print(f"Command: {report['command']}")
            print(f"Baseline: {'passed' if report['passed'] else 'failed'} (exit {report['exit_code']})")
            for node_id in report["failing"]:
                print(f"FAILED {node_id}")
        return 0
    if args.action == "survey":
        report = survey(args.repo_root)
        if args.json:
            print(json.dumps(report, ensure_ascii=False))
        else:
            print(f"Repository: {report['kind']}")
            paths = ", ".join(f"{item['path']} ({item['commits']} commits)" for item in report["hot_paths"])
            print(f"Hot paths: {paths or 'none'}")
            print(f"Existing setup: {', '.join(report['existing_setup']) or 'none'}")
            print("CI: " + (", ".join(
                f"{item['file']} (acceptance {'included' if item['runs_acceptance'] else 'excluded'})"
                for item in report["ci"]
            ) or "no test workflows"))
        return 0
    print(f"not implemented yet: {args.action}", file=sys.stderr)
    return 2
