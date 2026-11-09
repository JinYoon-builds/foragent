"""The documentation check for shelf places (shelf-places-documented, Jin r1317).

A session can only use a shelf place it has been told about. The check reads the shelf's place
list (``foragent.shelf.places()``) and the ``fa`` command tree (``fa <words> --help`` through
``foragent.cli.main``), then looks for each place in docs/mechanisms.md and in the skills of the
sessions that feed it.

"Named, with the fa command that feeds it" means one paragraph (text between blank lines, so a
list or a table counts as one paragraph) that holds the place's name as a whole word and an
``fa`` command that exists in the command tree and is not only a command group. The paragraph
must sit in a section (text between two headings) that talks about the shelf, so an incidental
word such as "logs" or "notes" next to an unrelated command does not count.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from functools import lru_cache
import io
from pathlib import Path
import re

from foragent import cli, shelf

ROOT = Path(__file__).resolve().parents[2]
MAP = "docs/mechanisms.md"
SESSIONS = {
    "planning": ("skills/fa", "skills/fa-order"),
    "orchestrator": ("skills/fa-orchestrator",),
}
IDEAS = "ideas"
# Places whose feeding command and feeding sessions the order names. Any other place, including
# one added later, must be named with some real fa command, in the map and in a planning or an
# orchestrator skill.
REQUIRED = {
    # Any session records a promise (orch_promise: "recorded by any session"); the order names the
    # /fa skill as missing it, and the orchestrator is the place's reader.
    "promises": ("fa orch promise add", ("planning", "orchestrator")),
    # Planning notes wait here for their boundary.
    "notes": ("fa order note", ("planning",)),
    # Waiting idea notes (`fa order note --kind idea`) are their own place, read by the PM
    # (handoff-ideas-place, PM r1435).
    IDEAS: ("fa order note", ("planning",)),
}
IDEA = "--kind idea"
SHELF = re.compile(r"\bshel(?:f|ves)\b", re.I)
FA = re.compile(r"(?<![\w/.-])fa((?:[ \t]+[a-z][a-z0-9-]*)+)")
HEADING = re.compile(r"(?m)^#{1,6} .*$")


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def map_texts() -> list[str]:
    return [read(MAP)]


def skill_texts(session: str) -> list[str]:
    texts = []
    for folder in SESSIONS[session]:
        texts += [path.read_text(encoding="utf-8") for path in sorted((ROOT / folder).rglob("*.md"))]
    return texts


def sections(text: str) -> list[tuple[str, list[str]]]:
    """(heading, paragraphs) per section; paragraphs have their whitespace folded."""
    found = []
    starts = [0, *(match.start() for match in HEADING.finditer(text)), len(text)]
    for start, end in zip(starts, starts[1:]):
        chunk = text[start:end]
        if not chunk.strip():
            continue
        lines = chunk.splitlines()
        heading = lines[0] if HEADING.match(lines[0] or "") else ""
        body = "\n".join(lines[1:] if heading else lines)
        paragraphs = [" ".join(part.split()) for part in re.split(r"\n[ \t]*\n", body) if part.strip()]
        found.append((heading, paragraphs))
    return found


def names(name: str, paragraph: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", paragraph, re.I) is not None


@lru_cache(maxsize=None)
def _help(words: tuple[str, ...]) -> str | None:
    """`fa <words> --help` output, or None when the command tree has no such command."""
    if not words or words[0] == "test":
        return None  # `fa test` hands its arguments to pytest instead of parsing them.
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            cli.main([*words, "--help"])
    except SystemExit as exc:
        return out.getvalue() if exc.code in (0, None) else None
    return None


def is_command(words: tuple[str, ...]) -> bool:
    """A real fa command that does something itself, not a group of subcommands."""
    text = _help(tuple(words))
    # argparse shows a subcommand group in its usage as "{a,b,c} ..." (a long usage wraps before "...").
    return text is not None and re.search(r"\{[\w,-]+\}\s+\.\.\.", text) is None


def commands(paragraph: str) -> set[str]:
    """Every real fa command named in a paragraph, as 'fa <words>'."""
    found = set()
    for match in FA.finditer(paragraph):
        words = match[1].split()
        for size in range(len(words), 0, -1):
            if is_command(tuple(words[:size])):
                found.add(" ".join(["fa", *words[:size]]))
                break
    return found


def documented(name: str, texts: list[str], command: str | None = None, *, extra: str | None = None) -> bool:
    """Whether a shelf section of the texts names the place with its command in one paragraph."""
    for text in texts:
        for heading, paragraphs in sections(text):
            if not SHELF.search(" ".join([heading, *paragraphs])):
                continue
            for paragraph in paragraphs:
                if not names(name, paragraph) or (extra is not None and extra not in paragraph):
                    continue
                found = commands(paragraph)
                if (command in found) if command is not None else found:
                    return True
    return False


def map_problems(places) -> list[tuple[str, str]]:
    problems = []
    for place in places:
        command = REQUIRED.get(place.name, (None, ()))[0]
        if not documented(place.name, map_texts(), command):
            problems.append((place.name, f"shelf place {place.name!r} is not named with "
                                         f"{command or 'the fa command that feeds it'} in {MAP}"))
    return problems


def skill_problems(places) -> list[tuple[str, str]]:
    problems = []
    for place in places:
        command, sessions = REQUIRED.get(place.name, (None, ()))
        if sessions:
            for session in sessions:
                if not documented(place.name, skill_texts(session), command):
                    problems.append((place.name, f"shelf place {place.name!r} is not named with {command} "
                                                 f"in the {session} skill ({', '.join(SESSIONS[session])})"))
        elif not any(documented(place.name, skill_texts(session)) for session in SESSIONS):
            problems.append((place.name, f"shelf place {place.name!r} is not named with the fa command that "
                                         "feeds it in the skill of the session that feeds it "
                                         f"({', '.join(folder for group in SESSIONS.values() for folder in group)})"))
    return problems


def problems(places=None) -> list[tuple[str, str]]:
    """The documentation check: (place name, message) for every place missing a mention."""
    places = shelf.places() if places is None else places
    return map_problems(places) + skill_problems(places)


def idea_documented(texts: list[str]) -> bool:
    """The shelf place 'ideas' named with `fa order note --kind idea` in one paragraph of a shelf section."""
    return documented(IDEAS, texts, "fa order note", extra=IDEA)


def new_place(name: str):
    """A place built by the shelf's own constructor, as a later issue would add it."""
    from datetime import timedelta

    return shelf.Place(name=name, purpose="Keep a new kind of waiting work", reader="orchestrator",
                       why="Waiting work nobody reads is lost", trigger="item recorded",
                       channel="orchestrator request box", alarm={"count": 5, "age": timedelta(days=1)},
                       capacity={"in": "items recorded", "out": "items done"}, removal="done",
                       person_load={"cap": 0, "form": "multiple-choice"})
