"""Entry point for repository adoption commands."""

import argparse
from collections import Counter
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys

import yaml

from foragent import gh
from foragent.config import Config, ConfigError, load_config
from foragent.git import GitError, GitRepo, slug_from_url
from foragent.processes import kill_process_group, observe_tree, recorded_tree


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
    return [command for _, command in _test_commands(script)]


def _test_commands(script: str) -> list[tuple[list[str], list[str]]]:
    """Keep invocation prefixes alongside normalized test commands."""
    script = script.replace("\\\n", "")
    try:
        commands = [shlex.split(script[start:end]) for start, end in _command_ranges(script)]
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
        invocation = tokens
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
            prefix = invocation[:len(invocation) - len(tokens) + 1]
            invocations.append((prefix, [program, *tokens[1:]]))
    return invocations


def _field(node: yaml.Node | None, key: str) -> yaml.Node | None:
    if isinstance(node, yaml.MappingNode):
        # Match the loader's last-value-wins behavior for duplicate keys.
        return next((value for name, value in reversed(node.value) if name.value == key), None)
    return None


def _workflow_runs(root: Path):
    """Read GitHub Actions run scalars with their original source positions."""
    for path in sorted((root / ".github/workflows").glob("*")):
        if not path.is_file() or path.suffix not in {".yml", ".yaml"}:
            continue
        source = path.read_bytes().decode("utf-8")
        loader = yaml.SafeLoader(source)
        try:
            workflow = loader.get_single_node()
            # Flatten YAML merge keys as safe_load does, retaining scalar marks.
            def flatten(node, seen):
                if node is None or id(node) in seen:
                    return
                seen.add(id(node))
                if isinstance(node, yaml.MappingNode):
                    loader.flatten_mapping(node)
                    for _, value in node.value:
                        flatten(value, seen)
                elif isinstance(node, yaml.SequenceNode):
                    for value in node.value:
                        flatten(value, seen)
            flatten(workflow, set())
        except yaml.YAMLError as exc:
            raise ConfigError(f"cannot read workflow {path}: {exc}") from exc
        finally:
            loader.dispose()
        jobs = _field(workflow, "jobs")
        if not isinstance(jobs, yaml.MappingNode):
            continue
        runs = []
        for _, job in jobs.value:
            steps = _field(job, "steps")
            if not isinstance(steps, yaml.SequenceNode):
                continue
            for step in steps.value:
                script = _field(step, "run")
                if isinstance(script, yaml.ScalarNode) and script.tag == "tag:yaml.org,2002:str":
                    runs.append(script)
        yield path, source, runs


def _ci(root: Path, acceptance_dir: str) -> list[dict]:
    reports = []
    for path, _, runs in _workflow_runs(root):
        invocations = [command for script in runs for command in _test_invocations(script.value)]
        if invocations:
            reports.append({
                "file": path.relative_to(root).as_posix(),
                "runs_acceptance": any(
                    command[0] != "pytest" or f"--ignore={acceptance_dir}" not in command[1:]
                    for command in invocations
                ),
            })
    return reports


def _command_ranges(script: str):
    """Locate shell command boundaries without splitting quoted arguments."""
    start = 0
    quote = None
    escaped = False
    comment = False
    for index, char in enumerate(script):
        if comment:
            if char == "\n":
                comment = False
                start = index + 1
        elif escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == "#" and (index == 0 or script[index - 1].isspace() or script[index - 1] in ";&|()"):
            yield start, index
            comment = True
        elif char == "&" and ((index > 0 and script[index - 1] in "><")
                              or script[index + 1:index + 2] == ">"):
            # Shell redirects such as 2>&1 and &>file are not command separators.
            continue
        elif char in ";&|()\n":
            yield start, index
            start = index + 1
    if not comment:
        yield start, len(script)


def _scalar_offsets(script: yaml.ScalarNode, source: str) -> list[int]:
    """Map decoded shell character boundaries back to the YAML source."""
    start, end = script.start_mark.index, script.end_mark.index
    # Scalar tokens start after optional YAML anchors and tags.
    token = next(token for token in yaml.scan(source[start:end]) if isinstance(token, yaml.ScalarToken))
    start += token.start_mark.index
    if script.style in {"'", '"'}:
        start += 1
        end -= 1
    offsets = [start]
    while start < end:
        length = 1
        if script.style == "'" and source[start:start + 2] == "''":
            length = 2
        elif script.style == '"' and source[start] == "\\":
            length = 2 + yaml.scanner.Scanner.ESCAPE_CODES.get(source[start + 1], 0)
        start += length
        offsets.append(start)
    return offsets


def ci(root: Path, exclude_acceptance: bool = False) -> dict:
    """Report CI changes, optionally inserting exclusions into the original text."""
    root = root.resolve()
    config = load_config(root) if (root / ".foragent.yml").is_file() else Config()
    ignore = f"--ignore={config.acceptance_dir.rstrip('/')}"
    report = {"needs_change": [], "changed": [], "manual": [], "collect_only": []}
    for path, source, runs in _workflow_runs(root):
        name = path.relative_to(root).as_posix()
        pytest_commands = [
            (prefix, command) for script in runs for prefix, command in _test_commands(script.value)
            if command[0] == "pytest"
        ]
        if pytest_commands and not any("--collect-only" in command[1:] for _, command in pytest_commands):
            report["collect_only"].append({
                "file": name, "add": shlex.join(pytest_commands[0][0]) + " --collect-only -q",
            })
        insertions = {}
        needs_change = False
        for script in runs:
            commands = [command for command in _test_invocations(script.value)
                        if command[0] != "pytest" or "--collect-only" not in command[1:]]
            missing = any(command[0] == "pytest" and ignore not in command[1:] for command in commands)
            needs_change |= missing
            multiline = (script.style in {"|", ">"} or "\n" in script.value
                         or script.start_mark.line != script.end_mark.line)
            if multiline:
                if missing or any(command[0] != "pytest" for command in commands):
                    report["manual"].append({"file": name, "line": script.value})
                continue
            offsets = _scalar_offsets(script, source) if missing else []
            for start, end in _command_ranges(script.value):
                line = script.value[start:end].rstrip()
                for command in _test_invocations(line):
                    if command[0] != "pytest":
                        report["manual"].append({"file": name, "line": line.strip()})
                    elif "--collect-only" not in command[1:] and ignore not in command[1:]:
                        addition = " " + shlex.quote(ignore)
                        if script.style == "'":
                            addition = addition.replace("'", "''")
                        elif script.style == '"':
                            addition = addition.replace("\\", "\\\\").replace('"', '\\"')
                        insertions[offsets[start + len(line)]] = addition
        if needs_change:
            report["needs_change"].append(name)
        if exclude_acceptance and insertions:
            # Inserting into the source preserves comments, quotes, CRLF, and EOF.
            for end, addition in sorted(insertions.items(), reverse=True):
                source = source[:end] + addition + source[end:]
            path.write_bytes(source.encode("utf-8"))
            report["changed"].append(name)
    return report


DOC_NAMES = ("CLAUDE.md", "AGENTS.md")
GH_LIST_LIMIT = 200
# GitHub's closing keywords; a bare "#N" mention is never evidence.
_CLOSING = re.compile(
    r"(?<![\w-])(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)(?::\s*|\s+)"
    r"(?:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#(\d+)\b",
    re.IGNORECASE,
)


def _docs(root: Path) -> list[dict]:
    """The project docs the survey read, each with a short opening excerpt."""
    names = sorted(path.name for path in root.glob("README*") if path.is_file())
    docs = []
    for name in [*names, *(name for name in DOC_NAMES if (root / name).is_file())]:
        text = (root / name).read_bytes().decode("utf-8", errors="replace")
        excerpt = ""
        for line in (line.strip() for line in text.splitlines()):
            if line:
                excerpt = f"{excerpt} {line}".strip()
                if len(excerpt) >= 300:
                    break
        docs.append({"path": name, "excerpt": excerpt[:300]})
    return docs


def closing_refs(message: str, slug: str | None) -> set[int]:
    """Issue numbers that a commit message closes with a GitHub closing keyword."""
    numbers = set()
    for match in _CLOSING.finditer(message):
        repo = match[1]
        if repo is None or (slug is not None and repo.lower() == slug.lower()):
            numbers.add(int(match[2]))
    return numbers


def _fixes(git: GitRepo, slug: str | None) -> dict[int, list[dict]]:
    """Commits reachable from HEAD by the issue numbers they close."""
    log = git._call(["log", "HEAD", "--format=%H%x00%s%x00%B%x1e"])
    found: dict[int, list[dict]] = {}
    for record in log.split("\x1e"):
        parts = record.lstrip("\n").split("\0", 2)
        if len(parts) != 3:
            continue
        sha, subject, body = parts
        for number in sorted(closing_refs(body, slug)):
            found.setdefault(number, []).append({"sha": sha, "subject": subject})
    return found


def _gh_list(args: list[str]) -> tuple[list[dict] | None, str | None]:
    try:
        result = gh.run(args, capture_output=True, text=True, timeout=60,
                        stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return None, "gh is not installed"
    except subprocess.TimeoutExpired:
        return None, f"gh {args[0]} list timed out"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return None, f"gh {args[0]} list failed: {detail[0] if detail else f'exit {result.returncode}'}"
    try:
        items = json.loads(result.stdout)
    except ValueError:
        return None, f"gh {args[0]} list returned unreadable JSON"
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        return None, f"gh {args[0]} list returned unexpected JSON"
    return items, None


def _github(git: GitRepo, has_commits: bool) -> dict:
    """Open issues and pull requests, read only; unavailable without a GitHub remote or gh."""
    try:
        slug = slug_from_url(git._call(["remote", "get-url", "origin"]))
    except GitError:
        slug = None
    report = {"repo": slug, "available": False, "reason": None,
              "issues": [], "pulls": [], "truncated": False}
    if slug is None:
        report["reason"] = "no GitHub remote"
        return report
    common = ["--repo", slug, "--state", "open", "--limit", str(GH_LIST_LIMIT)]
    issues, reason = _gh_list(["issue", "list", *common, "--json", "number,title,url,labels"])
    if issues is None:
        report["reason"] = reason
        return report
    pulls, reason = _gh_list(["pr", "list", *common, "--json", "number,title,url,headRefName"])
    if pulls is None:
        report["reason"] = reason
        return report
    fixes = _fixes(git, slug) if has_commits else {}
    report["available"] = True
    report["truncated"] = len(issues) >= GH_LIST_LIMIT or len(pulls) >= GH_LIST_LIMIT
    for item in issues:
        if not isinstance(item.get("number"), int):
            continue
        labels = [label.get("name") for label in item.get("labels") or [] if isinstance(label, dict)]
        report["issues"].append({
            "number": item["number"], "title": item.get("title", ""), "url": item.get("url", ""),
            "labels": [name for name in labels if isinstance(name, str)],
            "fixed_by": fixes.get(item["number"], []),
        })
    for item in pulls:
        if isinstance(item.get("number"), int):
            report["pulls"].append({"number": item["number"], "title": item.get("title", ""),
                                    "url": item.get("url", ""), "head": item.get("headRefName", "")})
    return report


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
        "docs": _docs(root),
        "github": _github(git, has_commits),
    }


def _detect_test_command(root: Path) -> str | None:
    """Follow order.context's command priority without needing its tracker."""
    if (root / "pyproject.toml").is_file() or (root / "requirements.txt").is_file():
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
    """Run existing tests once and report their state, including a red baseline."""
    root = root.resolve()
    command = command if command is not None else _detect_test_command(root)
    if not command:
        return {"command": None, "passed": None, "skipped": "no test command found"}
    config = load_config(root) if (root / ".foragent.yml").is_file() else Config()
    pytest = False
    # Insert before shell separators/comments so each pytest receives the flags.
    for start, end in reversed(list(_command_ranges(command))):
        line = command[start:end].rstrip()
        if any(args[0] == "pytest" for args in _test_invocations(line)):
            pytest = True
            offset = start + len(line)
            flags = " " + shlex.quote(f"--ignore={config.acceptance_dir.rstrip('/')}") + " -rf"
            command = command[:offset] + flags + command[offset:]
    with subprocess.Popen(
        ["/bin/sh", "-c", command], cwd=root, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
    ) as process:
        identities = observe_tree(roots=(process.pid,))
        try:
            output, _ = process.communicate(timeout=timeout)
            exit_code = process.returncode
        except subprocess.TimeoutExpired:
            # Kill the shell and its test runner, including inherited output pipes.
            with recorded_tree(identities):
                kill_process_group(process.pid)
            output, _ = process.communicate()
            exit_code = 124
    failing = []
    if pytest:
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output.decode("utf-8", errors="replace"))
        for line in text.splitlines():
            if line.startswith("FAILED "):
                node_id = line.removeprefix("FAILED ").split(" - ", 1)[0].strip()
                if node_id and node_id not in failing:
                    failing.append(node_id)
    return {"command": command, "exit_code": exit_code, "passed": exit_code == 0, "failing": failing}


def run(args: argparse.Namespace) -> int:
    if args.action == "baseline":
        report = baseline(args.repo_root, args.test_command, args.timeout)
        if args.json:
            print(json.dumps(report, ensure_ascii=False))
        elif report["command"] is None:
            print(f"Skipped: {report['skipped']}")
        else:
            print(f"Command: {report['command']}")
            print(f"Baseline: {'passed' if report['passed'] else 'failed'} (exit {report['exit_code']})")
            for node_id in report["failing"]:
                print(f"FAILED {node_id}")
        return 0
    if args.action == "ci":
        report = ci(args.repo_root, args.exclude_acceptance)
        if args.json:
            print(json.dumps(report, ensure_ascii=False))
        else:
            print(f"Needs change: {', '.join(report['needs_change']) or 'none'}")
            print(f"Changed: {', '.join(report['changed']) or 'none'}")
            for item in report["manual"]:
                print(f"Manual: {item['file']}: {item['line']}")
            for item in report["collect_only"]:
                print(f"Collect only: {item['file']}: add {item['add']}")
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
            print(f"Docs read: {', '.join(doc['path'] for doc in report['docs']) or 'none'}")
            github = report["github"]
            if not github["available"]:
                print(f"GitHub: not read ({github['reason']})")
            else:
                more = " or more" if github["truncated"] else ""
                fixed = [item for item in github["issues"] if item["fixed_by"]]
                print(f"Open issues ({len(github['issues'])}{more}, {len(fixed)} already fixed by a commit):")
                for item in github["issues"]:
                    evidence = ", ".join(f"{fix['sha'][:12]} {fix['subject']}" for fix in item["fixed_by"])
                    print(f"  #{item['number']} {item['title']}" + (f" (fixed by {evidence})" if evidence else ""))
                print(f"Open pull requests ({len(github['pulls'])}{more}):")
                for item in github["pulls"]:
                    print(f"  #{item['number']} {item['title']} ({item['head']})")
        return 0
    print(f"not implemented yet: {args.action}", file=sys.stderr)
    return 2
