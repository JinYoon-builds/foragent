"""Which tests a lane pull request's CI runs (Jin 11-08, r1227, CI capacity B).

A lane PR runs only the tests related to its changed files, chosen by the lanes'
related-test selection, plus the test files its Done when commands name. Every
other event (merge-train PRs, other PRs, pushes) and every lane PR that changes a
file the selection cannot map (CI workflows, test harness, configuration) runs the
full suite. ``None`` always means the full suite.
"""

from __future__ import annotations

from dataclasses import dataclass
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess

LANE_PREFIX = "foragent/issue-"
_ROW = re.compile(r"^\|\s*<code>(.*?)</code>\s*\|")
# The CI harness itself: the code that picks the tests (this module and the lanes' related
# selection in run_checks), runs them (the unit shard and the acceptance comparison with its
# measurement plugin and base cache) and generates the workflows. A change here cannot be
# judged by the tests related to it, so it widens to the full suite.
CI_HARNESS = frozenset(f"src/foragent/{name}.py" for name in (
    "related_ci", "run_checks", "ci_unit_shard", "ci_shard", "ci_pytest", "ci_base_cache", "ci_workflow"))


@dataclass(frozen=True)
class Selection:
    unit: tuple[str, ...]
    acceptance: tuple[str, ...]


def is_test_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return path.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def mappable(path: str) -> bool:
    """A changed file the related selection can map: a project module or a test file.

    Never the test harness: pytest configuration and plugins anywhere (harness_guard's
    names), non-test files under tests/, and the CI harness modules.
    """
    from foragent.harness_guard import HARNESS_NAMES

    pure = PurePosixPath(path)
    if pure.name.encode() in HARNESS_NAMES or path in CI_HARNESS:
        return False
    if pure.parts and pure.parts[0] == "tests":
        return is_test_file(path)
    return pure.suffix == ".py" and bool(pure.parts) and pure.parts[0] == "src"


def done_when_commands(body: str) -> list[str]:
    """The Done when commands of the gate's PR body table (run_pr_body.pr_body)."""
    commands, inside = [], False
    for line in (body or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("| Done when command |"):
            inside = True
            continue
        if not inside:
            continue
        if not stripped.startswith("|"):
            if commands:
                break
            continue
        if match := _ROW.match(stripped):
            commands.append(html.unescape(match.group(1).replace("<br>", "\n")))
    return commands


def command_paths(command: str, root: Path) -> list[str] | None:
    """Test files a pytest command names; None when a named path cannot be resolved."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    start = next((index for index, token in enumerate(tokens) if PurePosixPath(token).name == "pytest"), None)
    if start is None:
        return []
    from foragent.run_checks import PYTEST_OPTION_VALUES, PYTEST_SELECTION_VALUES

    paths = []
    words = iter(tokens[start + 1:])
    for token in words:
        if token.startswith("-"):
            if token in PYTEST_OPTION_VALUES | PYTEST_SELECTION_VALUES:
                next(words, None)  # `--junitxml PATH`: the value is never a selector.
            continue
        relative = token.split("::", 1)[0]
        if Path(relative).is_absolute():
            try:
                relative = Path(relative).resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                continue  # Outside the repository: not one of its tests.
        candidate = root / relative
        if candidate.is_file() and relative.endswith(".py"):
            paths.append(PurePosixPath(relative).as_posix())
        elif candidate.is_dir():
            paths.extend(sorted(path.relative_to(root).as_posix() for path in candidate.rglob("*.py")
                                if is_test_file(path.name)))
        elif "/" in relative or relative.endswith(".py"):
            return None  # A named test path that is missing: never guess a narrower run.
    return paths


def select(root: Path, changed: list[str], commands: list[str], acceptance_dir: str) -> Selection | None:
    from foragent.run_checks import related_unit_tests

    if not changed or not all(mappable(path) for path in changed):
        return None
    acceptance = PurePosixPath(acceptance_dir.rstrip("/"))
    unit = set(related_unit_tests(root, changed, acceptance_dir))
    accepted = {path for path in changed if is_test_file(path) and PurePosixPath(path).is_relative_to(acceptance)}
    for command in commands:
        paths = command_paths(command, root)
        if paths is None:
            return None
        for path in paths:
            (accepted if PurePosixPath(path).is_relative_to(acceptance) else unit).add(path)
    existing = {path for path in unit | accepted if (root / path).is_file()}
    return Selection(tuple(sorted(existing & unit)), tuple(sorted(existing & accepted)))


def _changed(root: Path, base: str, head: str) -> list[str] | None:
    try:
        output = subprocess.run(["git", "diff", "--name-only", "--no-renames", f"{base}...{head}"], cwd=root,
                                check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return [line for line in output.splitlines() if line]


def event_selection(root: Path, acceptance_dir: str) -> Selection | None:
    """The selection for this Actions run's event; None (full suite) unless a lane PR."""
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request" or not os.environ.get("GITHUB_EVENT_PATH"):
        return None
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        pull = event["pull_request"]
        head_ref, base, head = pull["head"]["ref"], pull["base"]["sha"], pull["head"]["sha"]
        body = pull.get("body") or ""
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(head_ref, str) or not head_ref.startswith(LANE_PREFIX):
        return None
    changed = _changed(root, base, head)
    if changed is None:
        return None
    selection = select(root, changed, done_when_commands(body), acceptance_dir)
    if selection is not None:
        print(f"Lane PR CI runs related tests only: {len(selection.unit)} unit and "
              f"{len(selection.acceptance)} acceptance file(s) for {len(changed)} changed file(s).")
    else:
        print("Lane PR CI runs the full suite: a changed file cannot be mapped to related tests.")
    return selection
