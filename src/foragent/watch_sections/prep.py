"""Order preparation as text rows above the lane rows (#171).

Reads only ``snapshot.prep``, the facts ``fa order progress`` records. Without
them the section adds nothing and the lane rows show as usual. One row names the
order with its step word, the test count and, only once published, the issue
numbers; under it the step's scene: the decision ledger with its confirmation
question, or the chunk table with the five slots, the empty cells left and the
open questions.
"""

from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.order import PROGRESS_STEPS
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot

ORDER = 4  # Between the header and the lane rows, which follow without a gap.
KEEP = True  # Preparation stays on a short screen like the lane rows it continues into.

SLOTS = ("goal", "out_of_scope", "done_when", "lane", "skills")
MARKS = {"emoji": {"glyph": "📝", "filled": "✓", "empty": "·", "asked": "?"},
         "ascii": {"glyph": "[+]", "filled": "v", "empty": ".", "asked": "?"}}
CHUNK_CELLS = 20


def _cut(value: str, width: int, style: str = "") -> Line:
    """One line cut to the width with an ellipsis; a row never wraps."""
    from foragent import watch

    value = watch._clean(value)
    if watch._cells(value) > width:
        value = "".join(part for part, _ in watch._fit(watch._line(value), max(0, width - 1))).rstrip() + "…"
    return watch._line(value, style)


def _title_row(prep: dict[str, Any], snapshot: Snapshot, language: str, glyph: str, width: int) -> Line:
    """The step word, test count and published numbers always stay; titles give way first.

    Published issue titles go first, then the order title is cut with an ellipsis.
    """
    from foragent import watch

    step = prep.get("step") or "start"
    facts = [watch.text(f"prep_{step}", language)]
    tests = prep.get("tests")
    # A recorded count shows from the tests step on, even 0/0; before it only a non-zero one.
    reached = step in PROGRESS_STEPS and PROGRESS_STEPS.index(step) >= PROGRESS_STEPS.index("tests")
    if isinstance(tests, dict) and isinstance(tests.get("total"), int) and (reached or tests["total"]):
        facts.append(watch.text("prep_tests_count", language, done=tests.get("done", 0), total=tests["total"]))
    numbers = [number for number in prep.get("published") or [] if isinstance(number, int)]
    titled = [f"#{number} {title}" if (title := snapshot.titles.get(number)) else f"#{number}" for number in numbers]
    title = watch._clean(prep.get("title") or "")

    def joined(name: str, published: list[str]) -> str:
        return " · ".join([f"{glyph} {name}".rstrip(), *facts, *published])

    for published in (titled, [f"#{number}" for number in numbers]):
        if watch._cells(joined(title, published)) <= width:
            return watch._line(joined(title, published), "bold")
    rest = watch._cells(joined("", published)) - watch._cells(glyph)
    room = width - rest - watch._cells(glyph) - 2  # The glyph's space and the ellipsis.
    if room > 0:
        cut = "".join(part for part, _ in watch._fit(watch._line(title), room)).rstrip() + "…"
        return watch._line(joined(cut, published), "bold")
    return _cut(joined("", published), width, "bold")  # Too narrow even for the facts alone.


def _ledger(prep: dict[str, Any], language: str, width: int) -> list[Line]:
    from foragent import watch

    lines = [_cut(item, width) for item in prep.get("ledger") or []]
    from foragent.watch_sections.lanes import turn_within

    # The confirmation asks the person; it names them first so a narrow cut keeps the name.
    confirm = f"{turn_within(language, width)} · {watch.text('prep_ledger_confirm', language)}"
    return lines + [_cut(confirm, width, "yellow")] if lines else []


def _chunks(prep: dict[str, Any], language: str, marks: dict[str, str], width: int) -> list[Line]:
    from foragent import watch
    from foragent.watch_sections.lanes import turn_within

    chunks = [chunk for chunk in prep.get("chunks") or [] if isinstance(chunk, str)]
    if not chunks:
        return []
    cells = prep.get("cells") or {}
    labels = [watch.text(f"prep_slot_{slot}", language) for slot in SLOTS]
    heading = watch.text("prep_chunk_heading", language)
    first = min(CHUNK_CELLS, max(watch._cells(value) for value in [heading, *chunks]))
    slots = [max(2, watch._cells(label)) for label in labels]
    # The chunk column gives way before the five slot columns (one cell between columns).
    first = max(1, min(first, width - sum(slots) - len(slots)))
    widths = [first, *slots]
    lines = [watch._fit(row, width) for row in watch._columns([heading, *labels], widths, "bold")]
    questions = []
    for chunk in chunks:
        values = []
        for slot, label in zip(SLOTS, labels):
            cell = (cells.get(chunk) or {}).get(slot) or {}
            if cell.get("filled"):
                values.append(marks["filled"])
            elif cell.get("ask"):
                values.append(marks["asked"])
                name = "".join(part for part, _ in watch._columns([chunk], [first])[0]).rstrip()
                # An open cell question is the person's (the order interview); the name comes first.
                questions.append(turn_within(language, width) + " · " + watch.text(
                    "prep_question", language, chunk=name, slot=label, question=cell["ask"]))
            else:
                values.append(marks["empty"])
        lines += [watch._fit(row, width) for row in watch._columns([chunk, *values], widths)]
    left = prep.get("cells_left", 0)
    lines.append(_cut(watch.text("prep_cells_left", language, count=left), width))
    return lines + [_cut(question, width, "yellow") for question in questions]


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    from foragent.watch_sections.lanes import glyph_mode

    prep = snapshot.prep
    if not prep:
        return []
    language, width = config.language, context["width"]
    marks = MARKS[glyph_mode(config)]
    lines = [_title_row(prep, snapshot, language, marks["glyph"], width)]
    step = prep.get("step")
    if step == "ledger":
        lines += _ledger(prep, language, width)
    elif step in {"chunk", "cell"}:
        lines += _chunks(prep, language, marks, width)
    return lines
