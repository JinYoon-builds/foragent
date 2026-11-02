"""Which skills an executor really read, from its own log (#1619).

Two ordinary reads count when they succeed: the executor's file read tool on a
``skills/<name>/SKILL.md``, and a plain read command (`cat`, `sed -n`, `head` and the like)
of such a file, run as one simple command. A final report, a failed call or a path in prose
establishes nothing. Compound commands, redirections and early exits are not judged here; usage
counts are evidence, not security. This module is the only writer of ``Run.skills_used``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import TYPE_CHECKING, Any

from foragent.executors import _events

if TYPE_CHECKING:
    from foragent.state import StateStore

# Shell characters that expand when unquoted; a skill name holding one could stand for any skill.
_EXPANDS = frozenset("*?[]{}$`")
_OPERATORS = frozenset(";&|<>()\n")
# Each plain reader's flags, then its options that take a value.
# Short and long options of GNU and BSD alike; `sed -i`/`-I` edit, so they are absent.
_OPTIONS = {"cat": ("AbeEnstTuvl", ""), "head": ("qvz", "nc"), "tail": ("fFqvzr", "ncbs"),
            "sed": ("nErsuzla", "ef"), "nl": ("p", "bdfhilnsvw")}
_LONG = {
    "cat": ({"show-all", "number-nonblank", "show-ends", "number", "squeeze-blank", "show-tabs",
             "show-nonprinting"}, set()),
    "head": ({"quiet", "silent", "verbose", "zero-terminated"}, {"lines", "bytes"}),
    "tail": ({"quiet", "silent", "verbose", "zero-terminated", "follow", "retry"},
             {"lines", "bytes", "sleep-interval", "pid", "max-unchanged-stats"}),
    "sed": ({"quiet", "silent", "regexp-extended", "separate", "unbuffered", "null-data", "posix", "debug",
             "sandbox", "follow-symlinks"}, {"expression", "file", "line-length"}),
    "nl": ({"no-renumber"}, {"body-numbering", "section-delimiter", "footer-numbering", "header-numbering",
                             "line-increment", "join-blank-lines", "number-format", "number-separator",
                             "starting-line-number", "number-width"}),
}
_SHELLS = frozenset({"sh", "bash", "zsh", "dash"})


@dataclass(frozen=True)
class _Word:
    """One shell word after quoting, with the positions of its characters that stayed unquoted."""

    text: str
    open: frozenset[int] = frozenset()


def _words(command: str) -> list[_Word] | None:
    """Split one simple shell command as POSIX shells do; None for any operator or unbalanced quote.

    Quotes and backslashes are removed but remembered per character, so a quoted `#`, `;` or `[`
    stays an ordinary character, and only an unquoted `#` at a word's start begins a comment.
    """
    words: list[_Word] = []
    text: list[str] = []
    open_: set[int] = set()
    in_word, quote, index = False, None, 0

    def end() -> None:
        nonlocal text, open_, in_word
        if in_word:
            words.append(_Word("".join(text), frozenset(open_)))
        text, open_, in_word = [], set(), False

    source = command.strip()
    while index < len(source):
        char = source[index]
        if quote == "'":
            if char == "'":
                quote = None
            else:
                text.append(char)
        elif quote == '"':
            if char == '"':
                quote = None
            elif char == "\\" and index + 1 < len(source) and source[index + 1] in '$`"\\\n':
                index += 1
                if source[index] != "\n":
                    text.append(source[index])
            else:
                if char in "$`":
                    open_.add(len(text))
                text.append(char)
        elif char in " \t":
            end()
        elif char == "\\":
            in_word = True
            if index + 1 < len(source):
                index += 1
                if source[index] != "\n":
                    text.append(source[index])
        elif char in "'\"":
            quote, in_word = char, True
        elif char == "#" and not in_word:
            break
        elif char in _OPERATORS:
            return None
        else:
            open_.add(len(text))
            text.append(char)
            in_word = True
        index += 1
    if quote is not None:
        return None
    end()
    return words


def _skill_name(word: _Word) -> str | None:
    """The folder of ``.../skills/<name>/SKILL.md``; another file is no skill.

    The path is resolved as written (`.` and `..` steps), as the reader resolved it. The recorded
    name must be literal: unquoted glob or variable characters there could stand for any skill,
    while ones in a parent folder cannot change which skill folder was read.
    """
    stack: list[tuple[str, int]] = []
    start = 0
    for segment in word.text.split("/"):
        if segment == "..":
            if stack:
                stack.pop()
        elif segment not in {"", "."}:
            stack.append((segment, start))
        start += len(segment) + 1
    if len(stack) < 3 or stack[-1][0] != "SKILL.md" or stack[-3][0] != "skills":
        return None
    name, begin = stack[-2]
    expanded = any(index in word.open and word.text[index] in _EXPANDS for index in range(begin, begin + len(name)))
    return None if expanded else name


def _plain_reads(command: Any) -> list[str]:
    """The skills a plain read command names; any other command counts as no read.

    Codex runs each command through `<shell> -lc "<command>"`, so that wrapper is opened first.
    """
    if not isinstance(command, str):
        return []
    words = _words(command)
    if not words:
        return []
    if len(words) == 3 and PurePosixPath(words[0].text).name in _SHELLS and words[1].text in {"-c", "-lc"}:
        return _plain_reads(words[2].text)
    program = PurePosixPath(words[0].text).name
    if program not in _OPTIONS:
        return []
    return [name for index in _files(program, [word.text for word in words[1:]])
            if (name := _skill_name(words[1 + index]))]


def _files(program: str, words: list[str]) -> list[int]:
    """The positions of one reader's file operands; option values and a `sed` script are not files.

    An option outside the reader's table (`sed -i`, `--help`) leaves the files in doubt: no read.
    """
    plain, valued = _OPTIONS[program]
    files: list[int] = []
    scripts = 0
    arguments = iter(enumerate(words))
    only_files = False
    for position, word in arguments:
        if only_files or word == "-" or not word.startswith("-"):
            files.append(position)
        elif word == "--":
            only_files = True
        elif word.startswith("--"):
            name, separator, _ = word[2:].partition("=")
            flags, takes_value = _LONG[program]
            if name in takes_value:
                if not separator:
                    next(arguments, None)  # The value is the next word.
                scripts += program == "sed" and name in {"expression", "file"}
            elif name not in flags or separator:
                return []
        elif program in {"head", "tail"} and word[1:].isdigit():
            continue
        else:
            rest = word[1:]
            while rest:
                letter, rest = rest[0], rest[1:]
                if letter in valued:
                    if not rest:
                        next(arguments, None)  # The value is the next word.
                    scripts += program == "sed"
                    break
                if letter not in plain:
                    return []
    if program == "sed" and not scripts:
        files = files[1:]  # Without -e or -f, the first operand is the script.
    return files


def _tool_reads(block: dict) -> list[str]:
    """The skills a Claude tool call reads if it succeeds."""
    tool, values = block.get("name"), block.get("input")
    if not isinstance(values, dict):
        return []
    if tool == "Read" and isinstance(values.get("file_path"), str):
        name = _skill_name(_Word(values["file_path"]))  # A read tool expands nothing.
        return [name] if name else []
    if tool == "Bash":
        return _plain_reads(values.get("command"))
    return []


def skills_read(log: Path, offset: int = 0) -> list[str]:
    """Each skill the log shows a successful read of, once, in the order first read."""
    read, waiting = [], {}
    for event in _events(log, offset=offset):
        item = event.get("item")
        if (event.get("type") == "item.completed" and isinstance(item, dict)
                and item.get("type") == "command_execution" and item.get("exit_code") == 0
                and item.get("status", "completed") == "completed"):
            read.extend(_plain_reads(item.get("command")))
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else ():
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and event.get("type") == "assistant" and block.get("id"):
                waiting[block["id"]] = _tool_reads(block)
            elif block.get("type") == "tool_result" and event.get("type") == "user" and block.get("tool_use_id"):
                names = waiting.pop(block["tool_use_id"], [])
                if not block.get("is_error"):
                    read.extend(names)
    return list(dict.fromkeys(read))


_writing = Lock()  # A review panel's reviewers finish at the same time; each adds to one list.


def record(store: StateStore, n: int, log: Path, offset: int) -> None:
    """Add what this session's part of the log shows; the session's own result is unaffected."""
    try:
        names = skills_read(log, offset) if log.exists() else []
    except OSError:
        return
    if not names:
        return
    with _writing:
        run = store.get_run(n)
        if run is not None:
            store.update_run(n, skills_reported=True, skills_used=list(dict.fromkeys([*run.skills_used, *names])))
