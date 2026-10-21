"""Design foundation discovery and raw color checks for UI files."""

from glob import iglob
import json
from pathlib import Path
import re


DEFAULT_TOKENS = ("**/tokens.css", "**/tokens.ts")
DEFAULT_GLOBS = ("**/*.css", "**/*.tsx", "**/*.jsx", "**/*.vue", "**/*.svelte", "**/*.html")
UI_FRAMEWORKS = frozenset({"react", "vue", "svelte", "next", "nuxt", "solid-js"})
COLOR = re.compile(
    r"(?<![\w#&])#(?:[0-9a-f]{8}|[0-9a-f]{6}|[0-9a-f]{4}|[0-9a-f]{3})(?![\w-])"
    r"|\b(?:rgb|hsl)\(",
    re.IGNORECASE,
)


def package_frameworks(path: Path) -> set[str]:
    """Read UI dependencies, including build-time dependencies, without running Node."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    frameworks = set()
    for section in ("dependencies", "devDependencies"):
        dependencies = data.get(section)
        if isinstance(dependencies, dict):
            frameworks.update(UI_FRAMEWORKS.intersection(dependencies))
    return frameworks


def _files(root: Path, patterns: list[str] | tuple[str, ...]) -> set[Path]:
    return {
        path for pattern in patterns
        for name in iglob(pattern, root_dir=root, recursive=True, include_hidden=True)
        if (path := root / name).is_file()
    }


def lint(root: Path, *, tokens: list[str] | None = None, globs: list[str] | None = None) -> dict:
    """Report each raw color occurrence; token patterns replace the defaults when supplied."""
    root = root.resolve()
    token_files = {path.resolve() for path in _files(root, DEFAULT_TOKENS if tokens is None else tokens)}
    files = _files(root, DEFAULT_GLOBS if globs is None else globs)
    violations, skipped = [], []
    checked = 0
    for path in sorted(files):
        if path.resolve() in token_files:
            continue
        name = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            skipped.append(name)
            continue
        checked += 1
        for line_number, line in enumerate(content.splitlines(), 1):
            for match in COLOR.finditer(line):
                violations.append({"path": name, "line": line_number, "column": match.start() + 1,
                                   "literal": match[0]})
    return {"violations": violations, "checked_files": checked, "skipped_files": skipped}


def run(args) -> int:
    report = lint(args.repo_root, tokens=args.tokens, globs=args.globs)
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        for item in report["violations"]:
            print(f"{item['path']}:{item['line']}:{item['column']}: raw color {item['literal']}")
        for path in report["skipped_files"]:
            print(f"Skipped unreadable file: {path}")
        print(f"{len(report['violations'])} raw color literal(s) in {report['checked_files']} checked file(s).")
    return 1 if report["violations"] else 0
