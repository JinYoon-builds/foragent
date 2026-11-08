"""Acceptance tests for n90-preparation-docs.

The fa-order skill and the preparation-cycle docstrings must describe the input-based rule
for new preparation evidence: changes to the reviewed inputs need `fa order prepare` again,
while an unrelated main move or a lane-memory-only update does not by itself.

Text is compared sentence by sentence (line breaks joined, backticks dropped, case folded).
"""

from __future__ import annotations

import ast
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "fa-order" / "SKILL.md"
CYCLE = ROOT / "src" / "foragent" / "order_prepare_cycle.py"

NEGATION = r"(\b(not|no|never|without|neither|nor)\b|n't\b)"
# A claim that main movement itself requires new preparation / a new cycle.
MAIN_MOVE = r"(\bmain itself\b|\bnew main\b|\b(a|any|every) main (revision|move|movement|commit)s?\b)"
# The reviewed inputs that need new preparation evidence when they change.
INPUTS = {
    "the issue": r"\bissue\b",
    "the other plan issues": r"\bother\b[^.]*\bissues\b",
    "the shared spec": r"\bspec\b",
    "references": r"\breferences?\b",
    "risk notes": r"\brisk",
    "the resolved lane": r"\blane\b",
    "the reviewer context": r"\breviewer",
    "published locked tests": r"\block",
    "or helpers": r"\bhelpers?\b",
    "published": r"\bpublish",
}


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("`", "")).strip().lower()


def _sentences(text: str) -> list[str]:
    return [part for part in re.split(r"(?<=[.!?])\s+", _flat(text)) if part]


def _clauses(text: str) -> list[str]:
    return [part.strip() for sentence in _sentences(text) for part in sentence.split(";") if part.strip()]


def _windows(text: str, size: int) -> list[str]:
    parts = _sentences(text)
    return [" ".join(parts[i:i + n]) for n in range(1, size + 1) for i in range(len(parts) - n + 1)]


def _has(sentence: str, *words: str) -> bool:
    return all(re.search(word, sentence) for word in words)


def _cycle_docstrings() -> str:
    """The module docstring and the docstrings of the cycle functions identity and requirement."""
    tree = ast.parse(CYCLE.read_text(encoding="utf-8"))
    texts = [ast.get_docstring(tree) or ""]
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in ("identity", "requirement"):
            texts.append(ast.get_docstring(node) or "")
    return "\n\n".join(texts)


def _main_exemption(text: str) -> list[str]:
    return [s for s in _sentences(text)
            if _has(s, r"\bmain\b", NEGATION, r"(prepar|\bcycle)")]


def _memory_exemption(text: str) -> list[str]:
    return [s for s in _sentences(text)
            if _has(s, r"memory", NEGATION, r"(prepar|\bcycle)")]


def _main_requires(text: str) -> list[str]:
    return [c for c in _clauses(text)
            if _has(c, MAIN_MOVE, r"(prepar|\bcycle)") and not re.search(NEGATION, c)]


def test_skill_names_every_reviewed_input_as_a_reason_for_new_preparation():
    text = SKILL.read_text(encoding="utf-8")
    windows = [w for w in _windows(text, 3) if re.search(r"prepar", w)]
    best = max(windows, key=lambda w: sum(bool(re.search(p, w)) for p in INPUTS.values()))
    missing = [name for name, pattern in INPUTS.items() if not re.search(pattern, best)]
    assert not missing, (
        "The fa-order skill's preparation guidance must name, as reasons for new preparation "
        "evidence, changes to the issue, other plan issues, the shared spec and references, risk "
        "notes, the resolved lane and reviewer context, and the published locked tests or helpers "
        f"the order involves. Closest passage misses {missing}:\n{best}")


def test_skill_says_unrelated_main_move_needs_no_new_preparation():
    text = SKILL.read_text(encoding="utf-8")
    wrong = _main_requires(text)
    assert not wrong, f"The fa-order skill still says main movement itself needs preparation again: {wrong}"
    assert _main_exemption(text), (
        "The fa-order skill must state that an unrelated main move does not by itself require "
        "a new preparation cycle")


def test_skill_says_lane_memory_update_needs_no_new_preparation():
    text = SKILL.read_text(encoding="utf-8")
    assert _memory_exemption(text), (
        "The fa-order skill must state that a lane-memory-only update does not by itself "
        "require a new preparation cycle")


def test_cycle_docstring_says_unrelated_main_move_starts_no_new_cycle():
    text = _cycle_docstrings()
    wrong = _main_requires(text)
    assert not wrong, (
        f"The preparation-cycle docstrings still say a new main starts a new cycle: {wrong}")
    assert _main_exemption(text), (
        "The preparation-cycle docstring (module, identity or requirement) must state that an "
        f"unrelated main move does not by itself start a new preparation cycle:\n{text}")


def test_cycle_docstring_says_lane_memory_update_starts_no_new_cycle():
    text = _cycle_docstrings()
    assert _memory_exemption(text), (
        "The preparation-cycle docstring (module, identity or requirement) must state that a "
        f"lane-memory-only update does not by itself start a new preparation cycle:\n{text}")
