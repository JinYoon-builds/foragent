"""Acceptance: a decision marked implemented is not proposed as idle-capacity work
(order 2026-11-08 PM answers, issue decision-implemented; PM q91 option A).

#2333's idle-capacity filler (the gate tick, what `fa gate tick` runs) files one drafting
request for the oldest decisions decided with no order and writes one draft order per taken
decision under `<state>/drafts/<order-id>/plan.json`. D2, D3 and D4 are founding decisions the
product already implements; they are marked implemented in docs/decisions.md, and the filler
skips decisions so marked.

The mark's form is the implementation's choice. These tests therefore take D2, D3, D4 and D9
from this repository's own docs/decisions.md: each heading's section text plus every
`<!-- foragent:... -->` line in the ledger that names that decision. D9 is copied without any
such line, so in the fixture it is decided with no order and no mark. On current main D2-D4
carry no mark, so the filler takes D2 first.

Everything is observed through public entry points: `scheduler.tick`, `fa orch inbox --json`
and the state drafts folder. No real process starts and no signal is sent (the locked `shelf`
fixture uses a fake Popen and a fake process table).
"""

import json
import re
from pathlib import Path

from foragent.decisions import CITATION
from test_board_status_groups import offline_github  # noqa: F401
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_shelf_requests_notes_lessons import drafted, inbox, is_alarm, later, shelf, tick  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401

REPO = Path(__file__).resolve().parents[2]
LEDGER = REPO / "docs/decisions.md"
IMPLEMENTED = ("D2", "D3", "D4")
HEADING = re.compile(r"^### (D[1-9][0-9]*)\.", re.MULTILINE)
COMMENT = re.compile(r"^<!-- foragent:[^\r\n]*-->\s*$", re.MULTILINE)


def _sections(text):
    """Decision id -> its section text in the ledger, heading to next heading or level-2 title."""
    found = {}
    heads = list(HEADING.finditer(text))
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        body = text[head.start():end]
        cut = re.search(r"^## ", body, re.MULTILINE)
        found[head[1]] = (body[:cut.start()] if cut else body).rstrip() + "\n"
    return found


def _names(line, identity):
    """Whether a foragent comment line names this decision (as a JSON id or an exact citation)."""
    payload = re.search(r"(\{.*\}|\[.*\])", line)
    if payload:
        try:
            value = json.loads(payload[1])
        except ValueError:
            value = None
        ids = []
        if isinstance(value, dict):
            raw = value.get("id", value.get("ids", []))
            ids = raw if isinstance(raw, list) else [raw]
        elif isinstance(value, list):
            ids = value
        if identity in ids:
            return True
    return identity in CITATION.findall(line)


def _strip_marks(section, identity):
    return "".join(line for line in section.splitlines(keepends=True)
                   if not (COMMENT.match(line) and _names(line, identity)))


def repo_ledger(extra=""):
    """A fixture ledger built from this repository's D2, D3, D4 and (unmarked) D9."""
    text = LEDGER.read_text(encoding="utf-8")
    sections = _sections(text)
    for identity in (*IMPLEMENTED, "D9"):
        assert identity in sections, f"docs/decisions.md must keep {identity}"
    # Comment lines outside the decision sections (the ledger allows marks anywhere).
    outside = [line for line in COMMENT.findall(text)
               if not any(line in section for section in sections.values())]
    parts = ["# 결정 기록\n\n## 2026-10-20\n\n"]
    parts += [sections[identity] + "\n" for identity in IMPLEMENTED]
    parts += [line.strip() + "\n" for line in outside if any(_names(line, i) for i in IMPLEMENTED)]
    parts.append("\n" + extra)
    parts.append("\n" + _strip_marks(sections["D9"], "D9"))
    return "".join(parts)


def write_ledger(case, text):
    path = case.root / "docs/decisions.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def drafting_requests(case):
    """Open gate requests that name ledger decisions: (row, decisions named in its text)."""
    rows = []
    for row in inbox(case, pending=True):
        if is_alarm(row) or row.get("from") != "gate":
            continue
        named = list(dict.fromkeys(CITATION.findall(row["text"])))
        if named:
            rows.append((row, named))
    return rows


def run_filler(case):
    tick(case)
    later(case, minutes=1)
    tick(case)


def test_founding_decisions_marked_implemented_are_skipped_and_d9_is_proposed(shelf):
    case = shelf
    write_ledger(case, repo_ledger())
    run_filler(case)
    found = drafting_requests(case)
    assert len(found) == 1, f"Idle capacity files exactly one drafting request: {found}"
    row, named = found[0]
    for identity in IMPLEMENTED:
        assert identity not in named, (
            f"{identity} is marked implemented in docs/decisions.md and must not be proposed: {named}")
    assert named == ["D9"], f"D9, decided with no order and not marked, is the one proposed: {named}"
    made = drafted(case)
    assert not set(made) & set(IMPLEMENTED), f"No draft order is written for a decision marked implemented: {made}"
    assert len(made.get("D9", [])) == 1, f"D9 gets exactly one draft order: {made}"
    later(case, minutes=1)
    tick(case)
    assert len(drafting_requests(case)) == 1, "The drafting request is not filed a second time"
    assert {key: len(value) for key, value in drafted(case).items()} == {"D9": 1}, (
        "Later ticks never draft D2-D4 nor D9 twice")


def test_an_unmarked_decision_is_still_picked_oldest_first_beside_marked_ones(shelf):
    """Decisions without the mark are picked as today: an older unmarked D5 comes before D9."""
    case = shelf
    extra = "### D5. 2026-10-20 Unmarked founding default\nOpaque decision five with no order.\n"
    write_ledger(case, repo_ledger(extra))
    run_filler(case)
    found = drafting_requests(case)
    assert len(found) == 1, f"Idle capacity files exactly one drafting request: {found}"
    _, named = found[0]
    assert named == ["D5", "D9"], (
        f"Only the decisions without the mark are proposed, oldest first: {named}")
    made = drafted(case)
    assert {key: len(value) for key, value in made.items()} == {"D5": 1, "D9": 1}, (
        f"One draft order each for the unmarked decisions, none for D2-D4: {made}")
