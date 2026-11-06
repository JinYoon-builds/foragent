"""Sending a lesson keeps every earlier line of the lane's notebook (2026-11-06).

Since #2267, taking an exclusive lock writes the lock owner into the locked file.
The lesson append locked the notebook (MEMORY.new.md) itself, so each send emptied
the lane's pending lessons. The notebook holds only lesson lines.
"""

import json

from foragent.config import Config
from foragent.lanes import send_lesson


def notebook(tmp_path, text):
    folder = tmp_path / ".claude/agent-memory/fa-view"
    folder.mkdir(parents=True)
    (folder / "MEMORY.md").write_text("curated\n")
    path = folder / "MEMORY.new.md"
    path.write_text(text)
    return path


def test_two_sends_keep_earlier_lessons_and_add_no_other_lines(tmp_path):
    config = Config(lanes={"view": {"paths": ["view/**"], "agent": "fa-view"}})
    path = notebook(tmp_path, "- [id:a] first lesson (from #1, lane view)\n- [id:b] second (from #2, lane view)\n")
    send_lesson(tmp_path, config, "view", 7, "c", "Third lesson.")
    send_lesson(tmp_path, config, "view", 8, "d", "Fourth lesson.")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "- [id:a] first lesson (from #1, lane view)",
        "- [id:b] second (from #2, lane view)",
        "- [id:c] Third lesson. (from #7, lane view)",
        "- [id:d] Fourth lesson. (from #8, lane view)",
    ], lines
    for line in lines:
        try:
            json.loads(line)
        except ValueError:
            continue
        raise AssertionError(f"Lock owner data must not be written into the notebook: {line}")
