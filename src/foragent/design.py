"""Local design checks and UI framework detection."""

import json
from pathlib import Path
import re


DEFAULT_TOKENS = ("**/tokens.css", "**/tokens.ts")
DEFAULT_GLOBS = ("**/*.css", "**/*.tsx", "**/*.jsx", "**/*.vue", "**/*.svelte", "**/*.html")
UI_FRAMEWORKS = {"react", "vue", "svelte", "next", "nuxt", "solid-js"}
RAW_COLOR = re.compile(r"(?<![\w#])#(?:[0-9a-f]{6}|[0-9a-f]{3})(?!\w)|\b(?:rgb|hsl)\(", re.I)


def package_frameworks(path: Path) -> set[str]:
    """Read UI dependencies, including development dependencies, from a manifest."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    frameworks = set()
    for field in ("dependencies", "devDependencies"):
        dependencies = data.get(field)
        if isinstance(dependencies, dict):
            frameworks.update(UI_FRAMEWORKS.intersection(dependencies))
    return frameworks


def _matches(root: Path, patterns: list[str] | tuple[str, ...]) -> set[Path]:
    paths = set()
    for pattern in patterns:
        path = Path(pattern)
        base = Path(path.anchor) if path.is_absolute() else root
        relative = str(path.relative_to(base)) if path.is_absolute() else pattern
        matches = base.glob(relative)
        paths.update(match for match in matches if match.is_file())
    return paths


def lint(root: Path, tokens: list[str] | None = None, globs: list[str] | None = None) -> dict:
    """Report every raw color outside token files, in stable file/source order."""
    token_files = {path.resolve() for path in _matches(root, DEFAULT_TOKENS if tokens is None else tokens)}
    files = _matches(root, DEFAULT_GLOBS if globs is None else globs)
    violations = []
    for path in sorted(files):
        if path.resolve() in token_files:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for match in RAW_COLOR.finditer(line):
                filename = path.relative_to(root) if path.is_relative_to(root) else path
                violations.append({"file": filename.as_posix(), "line": number,
                                   "column": match.start() + 1, "literal": match[0]})
    return {"violations": violations}
