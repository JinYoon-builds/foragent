"""Which tests a lane pull request's CI runs (Jin 11-08, r1227, CI capacity B).

A lane PR runs only the tests related to its changed files, chosen by the lanes'
related-test selection, plus the test files its Done when commands name. Every
other event (merge-train PRs, other PRs, pushes) and every lane PR that changes a
file the selection cannot map (CI workflows, test harness, configuration) runs the
full suite. ``None`` always means the full suite.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess

# Module level on purpose: the selection rule and the option sets are harness code that
# harness_modules must see this module run.
from foragent.run_checks import PYTEST_OPTION_VALUES, PYTEST_SELECTION_VALUES, related_unit_tests

LANE_PREFIX = "foragent/issue-"
_ROW = re.compile(r"^\|\s*<code>(.*?)</code>\s*\|")
_MODULE_OPTION = re.compile(r"(?:^|[\s\"'\[,=])-[mp]\s*([A-Za-z_][\w.]*)")
_PYTEST_CONFIGS = ("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg")
_SKIPPED_DIRS = {".git", ".venv", "venv", "node_modules", ".foragent", "__pycache__"}


@dataclass(frozen=True)
class Selection:
    unit: tuple[str, ...]
    acceptance: tuple[str, ...]


def is_test_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return path.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _pytest_hooks() -> frozenset[str] | None:
    try:
        from _pytest import hookspec
    except ImportError:
        return None  # Without pytest's own hook list every top-level `pytest_*` function counts.
    return frozenset(name for name in dir(hookspec) if name.startswith("pytest_"))


_HOOK_DEF = re.compile(r"\bdef\s+(pytest_\w+)\s*\(")


def defines_pytest_hooks(source: bytes | str, hooks: frozenset[str] | None) -> bool:
    """A pytest plugin: the module implements a pytest hook, or carries plugin source in a
    string it writes out for pytest to load. Unparsable source counts as a plugin."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return True
    if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("pytest_")
           and (hooks is None or node.name in hooks) for node in tree.body):
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "pytest_" in node.value:
            try:
                ast.parse(node.value)
            except (SyntaxError, ValueError):
                # A template with placeholders: read its hook definitions as text.
                if any(hooks is None or name in hooks for name in _HOOK_DEF.findall(node.value)):
                    return True
                continue
            if defines_pytest_hooks(node.value, hooks):
                return True
    return False


def _imports(tree: ast.Module, module: str, *, package: bool) -> set[str]:
    """Every dotted name a module can import, inside functions too, and by `import_module("x")`."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                # `from . import x` in a package's __init__ resolves inside that package.
                parts = module.split(".") if package else module.split(".")[:-1]
                parts = parts[:len(parts) - (node.level - 1)] if node.level - 1 <= len(parts) else []
                base = ".".join([*parts, *([base] if base else [])])
            if base:
                names.add(base)
                names.update(f"{base}.{alias.name}" for alias in node.names)
        elif (isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant)
              and isinstance(node.args[0].value, str)
              and getattr(node.func, "attr", getattr(node.func, "id", None)) in {"import_module", "__import__"}):
            names.add(node.args[0].value)
    return names


def _plugin_names(tree: ast.Module) -> set[str]:
    """`pytest_plugins = [...]` of a conftest or plugin module."""
    names = set()
    for node in tree.body:
        if (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "pytest_plugins"
                                                 for target in node.targets)):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                continue
            names.update([value] if isinstance(value, str) else [item for item in value if isinstance(item, str)])
    return names


def _entry_point_plugins(root: Path) -> set[str]:
    """Modules registered as `pytest11` entry points; pytest loads them with no hook required."""
    import configparser
    import tomllib

    values = []
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        values.extend(((data.get("project") or {}).get("entry-points") or {}).get("pytest11", {}).values())
    except (OSError, ValueError, AttributeError):
        pass
    parser = configparser.ConfigParser()
    try:
        parser.read(root / "setup.cfg", encoding="utf-8")
        text = parser.get("options.entry_points", "pytest11", fallback="")
        values.extend(line.split("=", 1)[1] for line in text.splitlines() if "=" in line)
    except (configparser.Error, UnicodeError):
        pass
    return {str(value).split(":", 1)[0].strip() for value in values}


def _files(root: Path, pattern: str) -> list[Path]:
    return [path for path in root.rglob(pattern)
            if path.is_file() and not _SKIPPED_DIRS & set(path.relative_to(root).parts[:-1])]


def harness_modules(root: Path) -> frozenset[str]:
    """Source files under src/ that the test or CI harness itself can run.

    Roots: every conftest, pytest plugins (modules implementing a pytest hook or carrying
    plugin source, named by `-p` in pytest configuration or a workflow, listed in a
    `pytest_plugins` or registered as a `pytest11` entry point) and modules a workflow step
    runs (`python -m`). The harness can run whatever a root or a module it reaches imports,
    inside functions too, and the packages above each module. When unsure, a module is
    harness: a lane PR changing it runs the full suite.
    """
    modules = {}
    for path in _files(root / "src", "*.py") if (root / "src").is_dir() else []:
        parts = path.relative_to(root / "src").with_suffix("").parts
        package = parts[-1] == "__init__"
        modules[".".join(parts[:-1] if package else parts)] = (path, package)

    def parse(path):
        try:
            return ast.parse(path.read_bytes())
        except (OSError, SyntaxError, ValueError):
            return None

    hooks = _pytest_hooks()
    named = set(_entry_point_plugins(root))
    for config in _PYTEST_CONFIGS:
        if (root / config).is_file():
            text = (root / config).read_text(encoding="utf-8", errors="replace")
            named.update(match.group(1) for match in _MODULE_OPTION.finditer(text) if "-p" in match.group(0))
    for workflow in [*_files(root / ".github/workflows", "*.yml"), *_files(root / ".github/workflows", "*.yaml")] \
            if (root / ".github/workflows").is_dir() else []:
        named.update(match.group(1) for match in
                     _MODULE_OPTION.finditer(workflow.read_text(encoding="utf-8", errors="replace")))
    for conftest in _files(root, "conftest.py"):
        if (parsed := parse(conftest)) is not None:
            named.update(_plugin_names(parsed))
            named.update(_imports(parsed, "conftest", package=False))
    named.update(name for name, (path, _) in modules.items()
                 if parse(path) is None or defines_pytest_hooks(path.read_bytes(), hooks))
    seen, stack = set(), [name for name in named if name in modules]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        parts = name.split(".")
        stack.extend(".".join(parts[:index]) for index in range(1, len(parts)))  # Importing runs each parent package.
        path, package = modules[name]
        if (parsed := parse(path)) is not None:
            stack.extend(_imports(parsed, name, package=package) | _plugin_names(parsed))
        stack[:] = [item for item in stack if item in modules]
    return frozenset(modules[name][0].relative_to(root).as_posix() for name in seen)


def mappable(path: str, harness: frozenset[str] = frozenset()) -> bool:
    """A changed file the related selection can map: a project module or a test file.

    Never the test harness: pytest configuration and plugins anywhere (harness_guard's
    names), non-test files under tests/, and the source modules the harness runs.
    """
    from foragent.harness_guard import HARNESS_NAMES

    pure = PurePosixPath(path)
    if pure.name.encode() in HARNESS_NAMES or path in harness:
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


def select(root: Path, changed: list[str], commands: list[str], acceptance_dir: str,
           base_source=lambda path: None) -> Selection | None:
    """``base_source(path)`` gives a changed file's base content, so removing a plugin's hooks still widens."""
    if not changed or any(PurePosixPath(path).parts[:1] == ("src",) and not (root / path).is_file()
                          for path in changed):
        return None  # A deleted or renamed source module: what ran it is not visible at the head.
    harness = harness_modules(root)
    if not all(mappable(path, harness) for path in changed):
        return None
    hooks = _pytest_hooks()
    if any(path.endswith(".py") and (old := base_source(path)) is not None and defines_pytest_hooks(old, hooks)
           for path in changed):
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
    def base_source(path):
        result = subprocess.run(["git", "show", f"{base}:{path}"], cwd=root, capture_output=True, check=False)
        return result.stdout if result.returncode == 0 else None

    selection = select(root, changed, done_when_commands(body), acceptance_dir, base_source)
    if selection is not None:
        print(f"Lane PR CI runs related tests only: {len(selection.unit)} unit and "
              f"{len(selection.acceptance)} acceptance file(s) for {len(changed)} changed file(s).")
    else:
        print("Lane PR CI runs the full suite: a changed file cannot be mapped to related tests.")
    return selection
