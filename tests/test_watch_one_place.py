"""A waiting sentence names only blockers whose acorn is not already on the screen."""

from datetime import datetime, timezone

from foragent import watch_scene
from foragent.watch_data import Snapshot


def test_wait_sentence_names_only_blockers_off_screen():
    snapshot = Snapshot(status={}, done={}, order=None, completed=0, total=0, eta=None, events=[],
                        captured_at=datetime(2026, 11, 6, tzinfo=timezone.utc),
                        waiting_facts={4: {"kind": None, "blocking_issues": [5, 11], "overlapping_files": []}})
    row = {"issue": 4}
    assert "#5, #11" in watch_scene.waiting_reason(snapshot, row, 4, "en")
    sentence = watch_scene.waiting_reason(snapshot, row, 4, "en", drawn={5})
    assert "#5" not in sentence and "#11" in sentence
    both = watch_scene.waiting_reason(snapshot, row, 4, "ko", drawn={5, 11})
    assert "#" not in both and both == watch_scene.text("lane_blocks_wait_after_shown", "ko")
