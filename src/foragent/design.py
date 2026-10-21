"""Local design checks and UI discovery for order planning."""

import argparse
import json
import os
from pathlib import Path
import re


DEFAULT_TOKENS = ("**/tokens.css", "**/tokens.ts")
DEFAULT_GLOBS = ("**/*.css", "**/*.tsx", "**/*.jsx", "**/*.vue", "**/*.svelte", "**/*.html")
UI_FRAMEWORKS = {"react", "vue", "svelte", "next", "nuxt", "solid-js"}
RAW_COLOR = re.compile(
    r"(?<![\w#])#(?:[0-9a-f]{6}|[0-9a-f]{3})(?![\w-])|(?<![\w-])(?:rgb|hsl)\(",
    re.IGNORECASE,
)


def ui_context(root: Path) -> dict:
    """Discover UI sources, including packages in a monorepo, without dependencies."""
    frameworks = set()
    ignored = {".git", ".venv", "node_modules", ".pytest_cache", "__pycache__"}
    for parent, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in ignored and not (Path(parent) / name).is_symlink())
        if any(Path(name).suffix.lower() == ".html" for name in files):
            frameworks.add("html")
        if "package.json" not in files:
            continue
        try:
            package = json.loads((Path(parent) / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(package, dict):
            for key in ("dependencies", "devDependencies"):
                dependencies = package.get(key)
                if isinstance(dependencies, dict):
                    frameworks.update(UI_FRAMEWORKS.intersection(dependencies))
    return {"frameworks": sorted(frameworks),
            "has_design_foundation": (root / "docs/design/CONTRACT.md").is_file()}


def _matched_files(root: Path, patterns: list[str] | tuple[str, ...]) -> set[Path]:
    files = set()
    for pattern in patterns:
        path = Path(pattern)
        base = Path(path.anchor) if path.is_absolute() else root
        glob = str(path.relative_to(path.anchor)) if path.is_absolute() else pattern
        files.update(match.resolve() for match in base.glob(glob) if match.is_file())
    return files


def lint(root: Path, *, tokens: list[str] | tuple[str, ...] = DEFAULT_TOKENS,
         globs: list[str] | tuple[str, ...] = DEFAULT_GLOBS) -> dict:
    """Report literal locations once per file; token and UI patterns are independent."""
    violations = []
    files = _matched_files(root, globs) - _matched_files(root, tokens)
    for path in sorted(files):
        relative = os.path.relpath(path, root)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for match in RAW_COLOR.finditer(line):
                violations.append({"path": relative, "line": number, "column": match.start() + 1,
                                   "literal": match[0]})
    return {"violations": violations}


def run(args: argparse.Namespace) -> int:
    report = lint(args.repo_root.resolve(), tokens=args.tokens or DEFAULT_TOKENS,
                  globs=args.globs or DEFAULT_GLOBS)
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        for item in report["violations"]:
            print(f"{item['path']}:{item['line']}:{item['column']}: raw color {item['literal']}")
        count = len(report["violations"])
        print(f"Design lint: {count} raw color literal(s) outside token files.")
    return 1 if report["violations"] else 0
