"""Resolve only the first executable of each Done when, without running it."""

import os
from pathlib import Path
import re
import shlex
import shutil
import tomllib

from foragent import processes
from foragent.i18n import text


# These are supplied by /bin/sh rather than found on PATH. Compound shell
# constructs are left to the shell; examining commands inside them is out of scope.
SHELL_WORDS = set("""! ( { if for while until case function
    : . break continue return exit export readonly set shift trap unset
    alias unalias bg cd command eval exec fc fg getopts hash jobs kill
    pwd read test times type ulimit umask wait true false printf echo [""".split())
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*=")


def _words(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:
        # Shell syntax and success are not part of this preflight.
        return []
    while words and ASSIGNMENT.match(words[0]):
        words.pop(0)
    return words


def _which(word: str, root: Path, env: dict[str, str]) -> str | None:
    if "/" in word:
        candidate = root / word
        return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
    # Relative PATH entries belong to the checks' working directory, not ours.
    path = os.pathsep.join(str(root / entry) for entry in
                           env.get("PATH", os.defpath).split(os.pathsep))
    return shutil.which(word, path=path)


def _uv_project(root: Path) -> bool:
    if (root / "uv.lock").is_file():
        return True
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text())
    except (OSError, ValueError):
        return False
    tool = data.get("tool")
    return bool(data.get("dependency-groups") or isinstance(tool, dict) and "uv" in tool)


def check(plan: dict, root: Path, language: str) -> None:
    from foragent.order import DoneWhenExecutableError

    env = processes._child_environment(root)
    runner = _uv_project(root) and _which("uv", root, env)
    lines = []
    for kind in ("issues", "edits"):
        for item in plan.get(kind, []):
            label = item["key"] if kind == "issues" else f"#{item['number']}"
            for command in item["done_when"]:
                words = _words(command)
                if not words or words[0] in SHELL_WORDS or _which(words[0], root, env):
                    continue
                suggestion = ""
                if runner:
                    suggested = words
                    if words[0] in {"python", "python3"} and words[1:3] == ["-m", "pytest"]:
                        suggested = words[2:]
                    suggestion = text("order_executable_suggestion", language,
                                      command=shlex.join(["uv", "run", *suggested]))
                lines.append(text("order_executable_missing", language, issue=label,
                                  executable=" ".join(words[0].split()), command=" ".join(command.split()),
                                  suggestion=" ".join(suggestion.split()) if suggestion else ""))
    if lines:
        raise DoneWhenExecutableError(lines)
