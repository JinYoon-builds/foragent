"""Project UI discovery and a token-only color lint for Done when checks."""

from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import re


DEFAULT_TOKENS = ("**/tokens.css", "**/tokens.ts")
DEFAULT_GLOBS = ("**/*.css", "**/*.tsx", "**/*.jsx", "**/*.vue", "**/*.svelte", "**/*.html")
IGNORED_DIRS = {"venv", "node_modules", "dist", "build", "coverage", "__pycache__"}
FRAMEWORKS = {"react", "vue", "svelte", "next", "nuxt", "solid-js"}
COLOR = re.compile(r"(?<![\w#])#(?:[0-9a-f]{8}|[0-9a-f]{6}|[0-9a-f]{4}|[0-9a-f]{3})(?![\w-])|\b(?:rgb|hsl)\(", re.I)


class DesignError(ValueError):
    """A local design preview cannot be prepared or opened."""


def project_files(root: Path):
    """Prune dependencies, generated output, hidden folders and directory symlinks."""
    for parent, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not name.startswith(".")
                         and name not in IGNORED_DIRS and not (Path(parent) / name).is_symlink())
        for name in sorted(files):
            path = Path(parent) / name
            if not path.is_symlink():
                yield path


def _matches(path: Path, pattern: str) -> bool:
    """Match root-relative glob components; ** includes zero or more directories."""
    def match(parts, patterns):
        if not patterns:
            return not parts
        if patterns[0] == "**":
            return match(parts, patterns[1:]) or bool(parts and match(parts[1:], patterns))
        return bool(parts and fnmatchcase(parts[0], patterns[0]) and match(parts[1:], patterns[1:]))

    return match(path.parts, Path(pattern).parts)


def ui_context(root: Path) -> dict:
    frameworks = set()
    for path in project_files(root):
        if path.suffix.lower() == ".html":
            frameworks.add("html")
        if path.name != "package.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            for field in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                dependencies = data.get(field)
                if isinstance(dependencies, dict):
                    frameworks.update(FRAMEWORKS.intersection(dependencies))
    return {"frameworks": sorted(frameworks),
            "has_design_foundation": (root / "docs/design/CONTRACT.md").is_file()}


def lint(root: Path, *, tokens=None, globs=None) -> dict:
    token_patterns = DEFAULT_TOKENS if tokens is None else tokens
    patterns = DEFAULT_GLOBS if globs is None else globs
    report = {"violations": [], "scanned": 0, "skipped": []}
    for path in project_files(root):
        relative = path.relative_to(root)
        if not any(_matches(relative, pattern) for pattern in patterns):
            continue
        if any(_matches(path if Path(pattern).is_absolute() else relative, pattern)
               for pattern in token_patterns):
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            report["skipped"].append({"path": relative.as_posix(), "reason": str(exc)})
            continue
        report["scanned"] += 1
        for line_number, line in enumerate(source.splitlines(), 1):
            for match in COLOR.finditer(line):
                report["violations"].append({"path": relative.as_posix(), "line": line_number,
                                             "column": match.start() + 1, "literal": match[0]})
    return report


def run(args) -> int:
    report = lint(args.repo_root.resolve(), tokens=args.tokens, globs=args.globs)
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        for item in report["violations"]:
            print(f"{item['path']}:{item['line']}:{item['column']}: raw color {item['literal']}")
        for item in report["skipped"]:
            print(f"Skipped {item['path']}: {item['reason']}")
        print(f"Design lint: {len(report['violations'])} raw color literal(s) in {report['scanned']} file(s).")
    return 1 if report["violations"] else 0
