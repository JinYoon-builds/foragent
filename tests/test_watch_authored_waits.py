"""Issue-owned waits reach the frame through the one public reader and cached rows."""

from datetime import UTC, datetime

import pytest

from foragent import cli  # noqa: F401 - load scheduler before status (the existing import order)
from foragent import order_waiting, status, watch, watch_data
from foragent.config import Config
from foragent.state import StateStore
from foragent.tracker import FakeTracker

NOW = datetime(2026, 11, 1, 1, tzinfo=UTC)
WAIT = "## Waiting\nReason: Locale  choice pending\nNext condition: Record the locale\nResponsible role: orchestrator\n"
VALUES = ("Locale  choice pending", "Record the locale", "orchestrator")


def acorns(layout):
    """Count issue acorns, independently of the bubble's recent-merge mentions."""
    return [target[1] for *_, target in layout["hits"]
            if isinstance(target, tuple) and target[0] == "acorn"]


def scenario(tmp_path, monkeypatch, language="en"):
    config = Config(lanes={"work": {}}, language=language, view_glyphs="ascii")
    store, tracker = StateStore(tmp_path / "state", now=lambda: NOW), FakeTracker()
    for n, labels, body, state in (
        (1, ["approved", "ready"], "", "open"),
        (2, [], WAIT, "open"), (3, ["blocked"], WAIT, "open"),
        (4, ["approved", "ready"], WAIT, "open"), (5, [], WAIT, "closed"),
        (6, ["approved", "ready"], "", "open"), (7, ["approved", "ready"], "", "open"),
    ):
        tracker.add_issue(n, f"Task {n}", body, ["lane:work", *labels], state=state)
    store.refresh_local_issues(tracker.list_issues("all"), [], config.lanes, NOW)
    monkeypatch.setattr(watch_data, "meter", lambda *a: {"running": 0, "budget": 2})
    return config, store, tracker


@pytest.mark.parametrize("language,labels", [
    ("en", ("Waiting:", "Next:", "Owner:")),
    ("ko", ("기다리는 이유:", "다음 조건:", "담당 역할:")),
])
def test_every_authored_wait_is_visible_folded_and_clear_follows_snapshot(tmp_path, monkeypatch, language, labels):
    config, store, tracker = scenario(tmp_path, monkeypatch, language)
    payload = status.collect(store, config, NOW)
    snapshot = watch.collect(payload, None, store, config, NOW)
    layout = {}
    frame = watch.render_frame(snapshot, config, tmp_path, width=240, layout=layout)
    explanations = "\n".join(line for line in frame.splitlines() if line.strip().startswith(labels))
    for value in (*VALUES, *labels):
        assert explanations.count(value) == 3
    for n in (2, 3, 4):
        assert acorns(layout).count(n) == 1
        assert snapshot.waiting_facts[n]["authored"]["reason"] == VALUES[0]
    assert "#5" not in frame and 5 not in snapshot.waiting_facts
    assert snapshot.waiting_facts[4]["kind"] is None  # Existing queue facts survive.
    assert "authored" not in snapshot.waiting_facts[1]
    assert "authored" not in str(payload)  # status JSON keeps its existing shape.
    # The scene's text fallback keeps every acorn and wait visible in either fold state.
    layout = {}
    unfolded = watch.render_frame(snapshot, config, tmp_path, width=240, state={"unfolded": {"work"}}, layout=layout)
    assert sorted(acorns(layout)) == [1, 2, 3, 4, 6, 7]
    unfolded = "\n".join(line for line in unfolded.splitlines() if line.strip().startswith(labels))
    for value in VALUES:
        assert unfolded.count(value) == 3
    tracker.update_issue(2, title="Task 2", body="## Waiting\nNone.\n")
    tracker.update_issue(4, title="Task 4", body=WAIT.replace(VALUES[0], "New reason"))
    store.refresh_local_issues(tracker.list_issues("all"), [], config.lanes, NOW)
    updated = watch.collect(payload, None, store, config, NOW)
    assert 2 not in updated.waiting_facts
    assert updated.waiting_facts[4]["authored"]["reason"] == "New reason"
    updated_frame = watch.render_frame(updated, config, tmp_path, width=240)
    assert "#2" not in updated_frame and updated_frame.count("New reason") == 1
    assert snapshot.waiting_facts[4]["authored"]["reason"] == VALUES[0]


def test_wait_reader_is_the_only_authority_and_unreadable_snapshot_is_absent(tmp_path, monkeypatch):
    config, store, tracker = scenario(tmp_path, monkeypatch)
    payload = status.collect(store, config, NOW)
    monkeypatch.setattr(order_waiting, "section", lambda body: None)
    snapshot = watch.collect(payload, None, store, config, NOW)
    assert not any(fact.get("authored") for fact in snapshot.waiting_facts.values())
    assert VALUES[0] not in watch.render_frame(snapshot, config, tmp_path, width=240)

    def unreadable():
        raise PermissionError("issues.json")

    monkeypatch.setattr(store, "local_issues", unreadable)
    assert watch.collect(payload, None, store, config, NOW).waiting_facts == {}


@pytest.mark.parametrize("authored", ["Reason: Choice  pending", "Wait for Jin's locale choice.\nOwner: planning",
                                    "Reason: A\nNext condition: B\nResponsible role: C\nContext: keep this too"])
@pytest.mark.parametrize("language", ["en", "ko"])
def test_partial_and_free_form_waits_keep_the_canonical_authored_text(tmp_path, monkeypatch, authored, language):
    config, store, tracker = scenario(tmp_path, monkeypatch, language)
    tracker.update_issue(2, title="Task 2", body="## Waiting\n" + authored)
    store.refresh_local_issues(tracker.list_issues("all"), [], config.lanes, NOW)
    snapshot = watch.collect(status.collect(store, config, NOW), None, store, config, NOW)
    assert snapshot.waiting_facts[2]["authored"] == {"text": authored}
    layout = {}
    frame = watch.render_frame(snapshot, config, tmp_path, width=240, layout=layout)
    assert acorns(layout).count(2) == 1
    for line in authored.splitlines():
        assert frame.count(line) == 1


@pytest.mark.parametrize("language", ["en", "ko"])
@pytest.mark.parametrize("structured", [False, True])
def test_authored_tabs_keep_visible_word_spacing_in_both_record_forms(tmp_path, monkeypatch, language, structured):
    config, store, tracker = scenario(tmp_path, monkeypatch, language)
    value = "Await\tlocale choice."
    body = WAIT.replace(VALUES[0], value) if structured else "## Waiting\n" + value
    tracker.update_issue(2, title="Task 2", body=body)
    store.refresh_local_issues(tracker.list_issues("all"), [], config.lanes, NOW)
    snapshot = watch.collect(status.collect(store, config, NOW), None, store, config, NOW)
    assert "\t" in snapshot.waiting_facts[2]["authored"]["text"]
    frame = watch.render_frame(snapshot, config, tmp_path, width=240)
    assert frame.count("Await   locale choice.") == 1


def test_reopened_issue_shows_its_current_wait_once_instead_of_a_done_row(tmp_path, monkeypatch):
    config, store, tracker = scenario(tmp_path, monkeypatch)
    store.start_run(2, branch="old", worktree="old", lane="work")
    store.finish_run(2, "merged")
    tracker.update_issue(2, title="Task 2", body=WAIT.replace(VALUES[0], "Reopened locale decision"))
    store.refresh_local_issues(tracker.list_issues("all"), [], config.lanes, NOW)
    snapshot = watch.collect(status.collect(store, config, NOW), None, store, config, NOW)
    for state in (None, {"unfolded": {"work"}}):
        layout = {}
        frame = watch.render_frame(snapshot, config, tmp_path, width=240, state=state, layout=layout)
        own = [frame.splitlines()[y] for _, y, _, _, target in layout["hits"] if target == ("acorn", 2)]
        assert len(own) == 1 and "[#2]" in own[0] and "done" not in own[0]
        assert frame.count("Waiting: Reopened locale decision") == 1


@pytest.mark.parametrize("active", [False, True])
def test_a_wait_follows_its_current_row_after_an_issue_changes_lane(tmp_path, monkeypatch, active):
    config, store, tracker = scenario(tmp_path, monkeypatch)
    config.lanes["other"] = {}
    store.start_run(2, branch="old", worktree="old", lane="work")
    if not active:
        store.finish_run(2, "merged")
    tracker.remove_labels(2, ["lane:work"])
    tracker.add_labels(2, ["lane:other", *(["approved", "running"] if active else [])])
    tracker.update_issue(2, title="Moved task", body=WAIT.replace(VALUES[0], "Moved locale decision"))
    store.refresh_local_issues(tracker.list_issues("all"), [], config.lanes, NOW)
    snapshot = watch.collect(status.collect(store, config, NOW), None, store, config, NOW)
    assert snapshot.waiting_facts[2]["lane"] == ("work" if active else "other")
    for state in (None, {"unfolded": {"work", "other"}}):
        layout = {}
        frame = watch.render_frame(snapshot, config, tmp_path, width=240, state=state, now=NOW, layout=layout)
        assert acorns(layout).count(2) == 1
        assert frame.count("Waiting: Moved locale decision") == 1


@pytest.mark.parametrize("labels", [["approved", "ready"], ["needs-human"]])
def test_other_current_rows_also_replace_a_previous_lanes_finished_row(tmp_path, monkeypatch, labels):
    config, store, tracker = scenario(tmp_path, monkeypatch)
    config.lanes["other"] = {}
    store.start_run(2, branch="old", worktree="old", lane="work")
    store.finish_run(2, "merged")
    tracker.remove_labels(2, ["lane:work"])
    tracker.add_labels(2, ["lane:other", *labels])
    tracker.update_issue(2, title="Moved task", body="")
    store.refresh_local_issues(tracker.list_issues("all"), [], config.lanes, NOW)
    snapshot = watch.collect(status.collect(store, config, NOW), None, store, config, NOW)
    for state in (None, {"unfolded": {"work", "other"}}):
        layout = {}
        watch.render_frame(snapshot, config, tmp_path, width=240, state=state, now=NOW, layout=layout)
        assert acorns(layout).count(2) == 1


@pytest.mark.parametrize("width", [20, 40, 80])
def test_long_waits_clip_in_cells_without_wrapping(tmp_path, monkeypatch, width):
    config, store, tracker = scenario(tmp_path, monkeypatch, "ko")
    tracker.update_issue(2, title="Task 2", body=WAIT.replace(VALUES[0], "한글 reason " * 40))
    store.refresh_local_issues(tracker.list_issues("all"), [], config.lanes, NOW)
    snapshot = watch.collect(status.collect(store, config, NOW), None, store, config, NOW)
    frame = watch.render_frame(snapshot, config, tmp_path, width=width)
    waits = [line for line in frame.splitlines() if "기다리는 이유:" in line]
    assert len(waits) == 3 and any(line.rstrip().endswith("...") for line in waits)
    assert all(watch._cells(line) <= width for line in frame.splitlines())
