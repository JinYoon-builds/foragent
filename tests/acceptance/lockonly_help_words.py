"""Shared text checks for the lockonly-help acceptance tests (UX r1102).

Text is compared sentence by sentence (line breaks joined, backticks dropped, case folded).
A claim may span two neighbouring sentences.
"""

from pathlib import Path
import re

import pytest

from foragent import cli

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "fa-order" / "SKILL.md"

# Words that together state the lock-only exception: an edit that changes only existing
# locked acceptance files publishes without test preparation, proved by the recheck.
LOCK_ONLY = (r"\bedit", r"\bonly\b", r"\bexisting\b", r"\block", r"\bacceptance\b",
             r"(\bwithout\b|\bno\b|\bnot\b|n't\b|\bskips?\b)", r"prepar", r"\brecheck")
# Words that state the covered-statements rule: when an edit is prepared, statements the
# issue's existing locked tests cover count as covered.
COVERED = (r"\bstatements?\b", r"\bexisting\b", r"\block", r"\bcover(ed|s)?\b.*\bcover(ed|s)?\b",
           r"prepar")


def _sentences(text: str) -> list[str]:
    flat = re.sub(r"\s+", " ", text.replace("`", "")).strip().lower()
    return [part for part in re.split(r"(?<=[.!?])\s+", flat) if part]


def _windows(text: str) -> list[str]:
    parts = _sentences(text)
    return parts + [f"{a} {b}" for a, b in zip(parts, parts[1:])]


def _states(text: str, words: tuple[str, ...]) -> bool:
    return any(all(re.search(word, window) for word in words) for window in _windows(text))


def _publish_help(capsys) -> str:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["order", "publish", "--help"])
    assert exit_info.value.code == 0
    return capsys.readouterr().out


def _statements_paragraph(help_text: str) -> str:
    lines = help_text.splitlines()
    start = next(i for i, line in enumerate(lines) if "edits[].statements" in line)
    end = next(i for i, line in enumerate(lines) if i > start and line.startswith("files lists"))
    return "\n".join(lines[start:end])


def _section(text: str, heading: str) -> str:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    end = next((i for i, line in enumerate(lines)
                if i > start and re.match(r"^##? ", line)), len(lines))
    return "\n".join(lines[start + 1:end])
