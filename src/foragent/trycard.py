"""Tolerant parsing of implementer-written, language-independent try card headings."""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class WebSpec:
    start: str
    port: int
    path: str
    steps: list[str]


@dataclass(frozen=True)
class TryCard:
    what_changed: str
    kind: str
    commands: list[str]
    expect: str
    web: WebSpec | None


def sections(text: str) -> dict[str, str]:
    """Ignore heading-like shell comments inside fenced blocks; last heading wins."""
    parts: dict[str, list[str]] = {}
    current: list[str] | None = None
    fence = None
    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if fence is not None:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
        elif marker:
            fence = marker[1]
        elif heading := re.fullmatch(r"\s*##[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*", line):
            current = parts[heading[1]] = []
            continue
        if current is not None:
            current.append(line)
    return {name: "\n".join(lines).strip() for name, lines in parts.items()}


def pr_try_text(body: str) -> str | None:
    """The PR's outer Try it section wraps a complete card with its own headings."""
    fence = None
    for match in re.finditer(r"^.*(?:\n|$)", body, re.MULTILINE):
        line = match[0].strip()
        marker = re.fullmatch(r"(`{3,}|~{3,})(.*)", line)
        if fence is not None:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
        elif marker:
            fence = marker[1]
        elif re.fullmatch(r"##[ \t]+Try it(?:[ \t]+#+)?[ \t]*", line):
            return body[match.end():]
    return None


def _commands(text: str) -> list[str]:
    commands = []
    script: list[str] = []
    fence = None
    for line in text.splitlines():
        marker = re.fullmatch(r"\s*(`{3,}|~{3,})(.*)", line)
        if fence is None:
            if marker:
                fence = marker[1]
                script = []
        elif marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
            if command := "\n".join(script).strip():
                commands.append(command)
            fence = None
        else:
            script.append(line)
    return commands


def parse_try(text: str) -> TryCard:
    parts = sections(text)
    instructions = parts.get("Try it", "")
    kind_match = re.search(r"^\s*kind:\s*(\w+)", instructions, re.MULTILINE)
    kind = "web" if kind_match and kind_match[1] == "web" else "cli"
    web = None
    if "Web" in parts:
        fields = {}
        steps = []
        in_steps = False
        for line in parts["Web"].splitlines():
            if field := re.fullmatch(r"\s*(start|port|path|steps):\s*(.*)", line):
                fields[field[1]] = field[2]
                in_steps = field[1] == "steps"
            elif in_steps and (step := re.fullmatch(r"\s*[-*+]\s+(.+)", line)):
                steps.append(step[1])
        try:
            port = int(fields.get("port", "0"))
        except ValueError:
            port = 0
        web = WebSpec(fields.get("start", ""), port, fields.get("path", ""), steps)
    return TryCard(
        parts.get("What changed", ""), kind, _commands(instructions),
        parts.get("Expect", ""), web,
    )
