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


# The gate writes each marker on its own line (order_waiting.on_own_line). Only such a line, outside
# a closed fence, is gate writing: marker text sharing a line or inside that fence is the author's.
_MARKER_LINE = re.compile(r"<!-- /?foragent:(?:(?!-->).)*-->")
_WAITING_HEADING = re.compile(r"##[ \t]+Waiting[ \t]*")
_WAITING_END = re.compile(r"#{1,2}(?:\s|$)|<!--\s*foragent:")


def _script_fence(line: str, fence: str | None) -> tuple[str | None, bool]:
    """The fence state after `line` under the rule for executable commands in a Done when section.

    Only backtick fences of three or four open here. Returns the state and whether `line` closed a fence.
    """
    if fence is not None:
        closed = re.fullmatch(rf"[ \t]*`{{{len(fence)},}}[ \t]*", line) is not None
        return (None if closed else fence), closed
    opening = re.fullmatch(r"[ \t]*(`{3,4})[^`]*", line)
    return (opening[1] if opening else None), False


def _fence_opening(line: str) -> str | None:
    """The Markdown fence `line` opens: backticks or tildes of any length."""
    opening = re.fullmatch(r"[ \t]*(?:(`{3,})[^`]*|(~{3,}).*)", line)
    return (opening[1] or opening[2]) if opening else None


def _closes_fence(fence: str, line: str) -> bool:
    return re.fullmatch(rf"[ \t]*{re.escape(fence[0])}{{{len(fence)},}}[ \t]*", line) is not None


def closed_fence_lines(lines: list[str]) -> set[int]:
    """Indexes of the lines inside a fence that closes, its fence lines included.

    A fence that never closes holds nothing here: gate writing is appended after whatever the
    body ends with, so it must stay recognisable after an unfinished fence. Waiting text is the
    gate's and ends where order_waiting ends it, so a fence line in it pairs with nothing later.
    """
    texts = [line.rstrip("\r\n") for line in lines]
    fenced: set[int] = set()
    never_closed: dict[str, int] = {}  # Shortest fence of each character known to have no closer.
    waiting = False
    index = 0
    while index < len(texts):
        text = texts[index]
        if waiting and not _WAITING_END.match(text):
            index += 1
            continue
        waiting = False
        fence = _fence_opening(text)
        if fence is not None and len(fence) < never_closed.get(fence[0], len(fence) + 1):
            closer = next((later for later in range(index + 1, len(texts)) if _closes_fence(fence, texts[later])), None)
            if closer is not None:
                fenced.update(range(index, closer + 1))
                index = closer + 1
                continue
            never_closed[fence[0]] = len(fence)
        waiting = _WAITING_HEADING.fullmatch(text) is not None
        index += 1
    return fenced


@dataclass(frozen=True)
class _Line:
    source: str  # As written in the body; sections and commands read this, so they do not depend on gate writing.
    authored: str  # Without gate writing: empty for a gate line.
    section: str | None  # The first section of a known name that holds this line.
    heading: bool = False


def _lines(body: str) -> list[_Line]:
    """Every line with its known section and authored text; line endings are already normalised.

    Headings inside any Markdown fence belong to the fence; a fence never closed runs to the end.
    Gate writing is the Waiting section and machine marker lines outside a closed fence. Waiting
    ends as order_waiting ends it, at the next `#`/`##` heading or foragent marker. Its text takes
    no part in fences (see closed_fence_lines), so it cannot hide what the author wrote after it.
    """
    lines: list[_Line] = []
    sources = body.splitlines(keepends=True)
    fenced = closed_fence_lines(sources)
    seen: set[str] = set()
    current: str | None = None
    fence: str | None = None
    waiting = False
    for index, source in enumerate(sources):
        line = source.removesuffix("\n")
        gate = False
        if waiting and _WAITING_END.match(line):
            waiting = False
        waiting_text = waiting
        if not waiting and index not in fenced:
            if _MARKER_LINE.fullmatch(line):
                gate = True
            elif _WAITING_HEADING.fullmatch(line):
                waiting = True
        heading = False
        if not waiting_text:
            if fence is not None:
                if _closes_fence(fence, line):
                    fence = None
            else:
                fence = _fence_opening(line)
                if fence is None and line.startswith("## "):
                    known = re.fullmatch(r"## (Goal|Behaviour|Out of scope|Files|Done when)[ \t]*", line)
                    current = known[1] if known and known[1] not in seen else None
                    seen.add(current or "")
                    heading = True
        lines.append(_Line(source, "" if gate or waiting else source, current, heading))
    return lines


def _section_lines(lines: list[_Line]) -> dict[str, list[_Line]]:
    """The known sections' lines, without their headings."""
    sections: dict[str, list[_Line]] = {}
    for line in lines:
        if line.section is not None:
            content = sections.setdefault(line.section, [])
            if not line.heading:
                content.append(line)
    return sections


def _sections(body: str) -> dict[str, list[str]]:
    """The known sections' authored text, by line; line endings are already normalised."""
    return {name: [line.authored for line in lines] for name, lines in _section_lines(_lines(body)).items()}


def authored_body(body: str) -> str:
    """The body as its author wrote it, without gate writing; comparing two shows authored changes."""
    return "".join(line.authored for line in _lines(body.replace("\r\n", "\n"))).strip()


def _done_when(lines: list[_Line]) -> tuple[list[str], list[ChecklistItem], list[str]]:
    """The Done when section's closed command fences, its checklist, and the authored text outside those fences."""
    commands: list[str] = []
    checklist: list[ChecklistItem] = []
    rest: list[str] = []
    fence = None
    held: list[_Line] = []
    for entry in lines:
        line = entry.source.removesuffix("\n")
        was_open = fence is not None
        fence, closed = _script_fence(line, fence)
        if closed:
            commands.append("".join(item.source for item in held[1:]).removesuffix("\n"))
            held = []
        elif fence is not None:
            held.append(entry)
        elif not was_open:
            rest.append(entry.authored)
            item = re.fullmatch(r"[ \t]*[-*][ \t]+\[([ xX])\][ \t]+(.*)", line)
            if item:
                checklist.append(ChecklistItem(item[2].strip(), item[1] != " "))
    return commands, checklist, rest + [item.authored for item in held]


def _other_content(lines: list[_Line], done_when_rest: list[str]) -> str:
    """Authored text the prompts do not already state on their own, in its original order."""
    kept: list[str] = []
    for line in lines:
        if line.section == "Done when":
            if line.heading and any(text.strip() for text in done_when_rest):
                kept += [line.authored, *done_when_rest]
        elif line.section not in {"Goal", "Out of scope"}:
            kept.append(line.authored)
    return "".join(kept).strip()


def _text(lines: list[_Line]) -> str:
    return "".join(line.authored for line in lines).strip()


def has_behaviour(body: str) -> bool:
    """Whether the body was published with behaviour statements (a real heading, not fenced text)."""
    return "Behaviour" in _sections(body.replace("\r\n", "\n"))


def parse_body(body: str) -> ParsedIssue:
    body = body.replace("\r\n", "\n")
    lines = _lines(body)
    sections = _section_lines(lines)
    commands, checklist, done_when_rest = _done_when(sections.get("Done when", []))

    return ParsedIssue(
        version="v1" if MARKER in body else None,
        goal=_text(sections.get("Goal", [])),
        out_of_scope=_text(sections.get("Out of scope", [])),
        done_when_commands=commands,
        done_when_checklist=checklist,
        exclusive=exclusive_paths(body),
        files=[match[1] for line in sections.get("Files", [])
               if (match := re.fullmatch(r"[ \t]*[-*] (.+)", line.authored.removesuffix("\n")))],
        other_content=_other_content(lines, done_when_rest),
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
