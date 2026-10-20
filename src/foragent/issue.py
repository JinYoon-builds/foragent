"""Parse the versioned issue body without executing its contents."""

from dataclasses import dataclass
import hashlib
import re
import shlex


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


def parse_body(body: str) -> ParsedIssue:
    body = body.replace("\r\n", "\n")
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    fence: str | None = None
    for raw_line in body.splitlines(keepends=True):
        line = raw_line.removesuffix("\n")
        if fence is not None:
            if re.fullmatch(rf"[ \t]*`{{{len(fence)},}}[ \t]*", line):
                fence = None
        else:
            opening = re.fullmatch(r"[ \t]*(`{3,4})[^`]*", line)
            if opening:
                fence = opening[1]
            elif line.startswith("## "):
                heading = re.fullmatch(r"## (Goal|Out of scope|Done when)[ \t]*", line)
                current = None
                if heading and heading[1] not in sections:
                    current = sections[heading[1]] = []
                continue
        if current is not None:
            current.append(raw_line)

    commands: list[str] = []
    checklist: list[ChecklistItem] = []
    fence = None
    script: list[str] = []
    for raw_line in sections.get("Done when", []):
        line = raw_line.removesuffix("\n")
        if fence is not None:
            if re.fullmatch(rf"[ \t]*`{{{len(fence)},}}[ \t]*", line):
                commands.append("".join(script).removesuffix("\n"))
                fence = None
            else:
                script.append(raw_line)
        else:
            opening = re.fullmatch(r"[ \t]*(`{3,4})[^`]*", line)
            item = re.fullmatch(r"[ \t]*[-*][ \t]+\[([ xX])\][ \t]+(.*)", line)
            if opening:
                fence = opening[1]
                script = []
            elif item:
                checklist.append(ChecklistItem(item[2].strip(), item[1] != " "))

    return ParsedIssue(
        version="v1" if MARKER in body else None,
        goal="".join(sections.get("Goal", [])).strip(),
        out_of_scope="".join(sections.get("Out of scope", [])).strip(),
        done_when_commands=commands,
        done_when_checklist=checklist,
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
