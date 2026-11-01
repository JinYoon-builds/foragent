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
    other_content: str = ""


# The gate writes a marker from the first column; an indented one, prose or a fenced example is authored.
_MARKER_LINE = re.compile(r"<!--\s*/?foragent:(?:(?!-->).)*-->[ \t]*")
# order_waiting removes these and appends them after the last line of every body it writes. Without
# a final newline they share that line, so only the body's tail can hold gate writing on an authored
# line; the same text anywhere else is the author's.
_GLUED_TAIL = re.compile(
    r"(?<=\S)(?:<!-- foragent:(?:waiting-prior sha256=[0-9a-f]{64}|write) id=[0-9a-f]* -->\s*)+\Z",
)
_WAITING_HEADING = re.compile(r"##[ \t]+Waiting[ \t]*")
_WAITING_END = re.compile(r"#{1,2}(?:\s|$)|<!--\s*foragent:")


def _script_fence(line: str, fence: str | None) -> tuple[str | None, bool]:
    """The fence state after `line`, under the rule that decides sections and executable commands.

    Only backtick fences of three or four open here. Returns the state and whether `line` closed a fence.
    """
    if fence is not None:
        closed = re.fullmatch(rf"[ \t]*`{{{len(fence)},}}[ \t]*", line) is not None
        return (None if closed else fence), closed
    opening = re.fullmatch(r"[ \t]*(`{3,4})[^`]*", line)
    return (opening[1] if opening else None), False


def _closes_block(block: str, line: str) -> bool:
    return re.fullmatch(rf"[ \t]*{re.escape(block[0])}{{{len(block)},}}[ \t]*", line) is not None


@dataclass(frozen=True)
class _Line:
    source: str  # As written in the body; sections and commands read this, so they do not depend on gate writing.
    authored: str  # Without gate writing: empty for a gate line.
    section: str | None  # The first section of a known name that holds this line.
    heading: bool = False


def _lines(body: str) -> list[_Line]:
    """Every line with its known section and authored text; line endings are already normalised.

    Gate writing is the Waiting section and machine markers outside any Markdown code fence: a
    backtick or tilde fence of any length. Waiting ends as order_waiting ends it, at the next
    `#`/`##` heading or foragent marker, whatever fence its own text holds: that text is the
    gate's, so a fence left open in it cannot hide the authored sections after it.
    """
    lines: list[_Line] = []
    seen: set[str] = set()
    current: str | None = None
    fence: str | None = None
    block: str | None = None
    waiting = False
    tail = _GLUED_TAIL.search(body)
    authored_end = tail.start() if tail else len(body)
    start = 0
    for source in body.splitlines(keepends=True):
        line = source.removesuffix("\n")
        bare = line[:max(authored_end - start, 0)]
        start += len(source)
        gate = False
        if block is not None:
            if _closes_block(block, bare):
                block = None
        else:
            if waiting and _WAITING_END.match(bare):
                waiting = False
            if not waiting:
                opening = re.fullmatch(r"[ \t]*(?:(`{3,})[^`]*|(~{3,}).*)", bare)
                if opening:
                    block = opening[1] or opening[2]
                elif _MARKER_LINE.fullmatch(bare):
                    gate = True
                elif _WAITING_HEADING.fullmatch(bare):
                    waiting = True
        heading = False
        was_open = fence is not None
        fence, _ = _script_fence(line, fence)
        if not was_open and fence is None and line.startswith("## "):
            known = re.fullmatch(r"## (Goal|Behaviour|Out of scope|Files|Done when)[ \t]*", line)
            current = known[1] if known and known[1] not in seen else None
            seen.add(current or "")
            heading = True
        authored = "" if gate or waiting else bare + source[len(line):]
        lines.append(_Line(source, authored, current, heading))
    return lines


def _sections(lines: list[_Line]) -> dict[str, list[_Line]]:
    """The known sections' lines, without their headings."""
    sections: dict[str, list[_Line]] = {}
    for line in lines:
        if line.section is not None:
            content = sections.setdefault(line.section, [])
            if not line.heading:
                content.append(line)
    return sections


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
    return "Behaviour" in _sections(_lines(body.replace("\r\n", "\n")))


def parse_body(body: str) -> ParsedIssue:
    body = body.replace("\r\n", "\n")
    lines = _lines(body)
    sections = _sections(lines)
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
