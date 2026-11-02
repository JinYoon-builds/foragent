"""Parse the versioned issue body without executing its contents."""

from dataclasses import dataclass, field
import hashlib
import re
import shlex

from foragent.issue_metadata import exclusive_paths


MARKER = "<!-- foragent:v1 -->"


@dataclass(frozen=True)
class ChecklistItem:
    text: str
    checked: bool


@dataclass(frozen=True)
class ParsedIssue:
    version: str | None
    goal: str
    out_of_scope: str
    done_when_commands: list[str]
    done_when_checklist: list[ChecklistItem]
    files: list[str] = field(default_factory=list)
    exclusive: list[str] = field(default_factory=list)
    # Authored content beside the first Goal, Out of scope and closed Done when commands, verbatim.
    # For prompts only: two bodies parse equal as long as the values the gate acts on are equal.
    other_content: str = field(default="", compare=False)


# The gate writes each marker on its own line (order_waiting.on_own_line).
_MARKER_LINE = re.compile(r"<!-- /?foragent:(?:(?!-->).)*-->")
# Snapshots approved before that rule can hold these two at the end of a line: the last line of a body
# without a final newline, which later approved content can leave anywhere in the body.
_GLUED_MARKERS = re.compile(
    r"(?<=.)(?:<!-- foragent:(?:waiting-prior sha256=[0-9a-f]{64}|write) id=[0-9a-f]{32} -->)+$"
)
_WAITING_HEADING = re.compile(r"##[ \t]+Waiting[ \t]*")
_WAITING_END = re.compile(r"#{1,2}(?:\s|$)|<!--\s*foragent:")


def _script_fence(line: str, fence: str | None) -> tuple[str | None, bool]:
    """The fence state after `line`, and whether `line` closed a fence."""
    if fence is not None:
        closed = re.fullmatch(rf"[ \t]*`{{{len(fence)},}}[ \t]*", line) is not None
        return (None if closed else fence), closed
    opening = re.fullmatch(r"[ \t]*(`{3,4})[^`]*", line)
    return (opening[1] if opening else None), False


@dataclass(frozen=True)
class _Line:
    authored: str  # The line as written; empty when it is gate writing.
    section: str | None  # The first section of a known name that holds this line.
    heading: bool = False


def _lines(body: str) -> list[_Line]:
    """Every line with its known section; line endings are already normalised.

    Gate writing is the Waiting section (ending as order_waiting ends it), machine marker lines and
    the glued markers above, outside fences. A fence never closed hides no gate writing, because the
    gate appends to the body.
    """
    sources = body.splitlines(keepends=True)
    inside: list[bool] = []
    content: list[bool] = []  # Between a fence's own lines; commands run from these as written.
    fence = None
    opened = 0
    for index, source in enumerate(sources):
        was_open = fence is not None
        fence, closed = _script_fence(_GLUED_MARKERS.sub("", source.removesuffix("\n")), fence)
        if fence is not None and not was_open:
            opened = index
        inside.append(was_open or fence is not None)
        content.append(was_open and not closed)
    unfinished = opened if fence is not None else len(sources)

    lines: list[_Line] = []
    seen: set[str] = set()
    current: str | None = None
    waiting = False
    for index, source in enumerate(sources):
        line = source.removesuffix("\n")
        ending = source[len(line):]
        gate = False
        if not content[index] or index >= unfinished:
            line = _GLUED_MARKERS.sub("", line)
        if not inside[index] or index >= unfinished:
            if waiting and _WAITING_END.match(line):
                waiting = False
            if not waiting:
                gate = _MARKER_LINE.fullmatch(line) is not None
                waiting = _WAITING_HEADING.fullmatch(line) is not None
        heading = not inside[index] and line.startswith("## ")
        if heading:
            known = re.fullmatch(r"## (Goal|Behaviour|Out of scope|Files|Done when)[ \t]*", line)
            current = known[1] if known and known[1] not in seen else None
            seen.add(current or "")
        lines.append(_Line("" if gate or waiting else line + ending, current, heading))
    return lines


def _sections(body: str) -> dict[str, list[str]]:
    """The body's known sections as authored lines, outside code fences; line endings are already normalised."""
    sections: dict[str, list[str]] = {}
    for line in _lines(body):
        if line.section is not None:
            content = sections.setdefault(line.section, [])
            if not line.heading:
                content.append(line.authored)
    return sections


def authored_body(body: str) -> str:
    """The body without gate writing; comparing two shows what the author changed."""
    return "".join(line.authored for line in _lines(body.replace("\r\n", "\n"))).strip()


def _other_content(body: str, done_when_rest: list[str]) -> str:
    """Authored text the prompts do not already state on their own, in its original order."""
    kept: list[str] = []
    for line in _lines(body):
        if line.section == "Done when":
            if line.heading and any(text.strip() for text in done_when_rest):
                kept += [line.authored, *done_when_rest]
        elif line.section not in {"Goal", "Out of scope"}:
            kept.append(line.authored)
    return "".join(kept).strip()


def has_behaviour(body: str) -> bool:
    """Whether the body was published with behaviour statements (a real heading, not fenced text)."""
    return "Behaviour" in _sections(body.replace("\r\n", "\n"))


def parse_body(body: str) -> ParsedIssue:
    body = body.replace("\r\n", "\n")
    sections = _sections(body)

    commands: list[str] = []
    checklist: list[ChecklistItem] = []
    rest: list[str] = []  # Done when lines outside closed command fences.
    fence = None
    held: list[str] = []
    for raw_line in sections.get("Done when", []):
        line = raw_line.removesuffix("\n")
        was_open = fence is not None
        fence, closed = _script_fence(line, fence)
        if closed:
            commands.append("".join(held[1:]).removesuffix("\n"))
            held = []
        elif fence is not None:
            held.append(raw_line)
        elif not was_open:
            rest.append(raw_line)
            item = re.fullmatch(r"[ \t]*[-*][ \t]+\[([ xX])\][ \t]+(.*)", line)
            if item:
                checklist.append(ChecklistItem(item[2].strip(), item[1] != " "))
    rest += held

    return ParsedIssue(
        version="v1" if MARKER in body else None,
        goal="".join(sections.get("Goal", [])).strip(),
        out_of_scope="".join(sections.get("Out of scope", [])).strip(),
        done_when_commands=commands,
        done_when_checklist=checklist,
        exclusive=exclusive_paths(body),
        files=[match[1] for raw_line in sections.get("Files", [])
               if (match := re.fullmatch(r"[ \t]*[-*] (.+)", raw_line.removesuffix("\n")))],
        other_content=_other_content(body, rest),
    )


def body_hash(body: str) -> str:
    normalized = body.replace("\r\n", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def has_machine_check(parsed: ParsedIssue) -> bool:
    return any(command.strip() for command in parsed.done_when_commands)


def acceptance_paths(commands: list[str], acceptance_dir: str) -> list[str]:
    def normalize(path: str) -> str:
        while path.startswith("./"):
            path = path[2:]
        return path.rstrip("/")

    directory = normalize(acceptance_dir)
    paths: set[str] = set()
    for command in commands:
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;()")
            lexer.whitespace_split = True
            lexer.commenters = ""
            tokens = list(lexer)
        except ValueError:
            tokens = [token.strip("\"'") for token in command.split()]
        for token in tokens:
            path = normalize(token.split("::", 1)[0].rsplit("=", 1)[-1])
            if path == directory or path.startswith(directory + "/"):
                paths.add(path)
    return sorted(paths)
