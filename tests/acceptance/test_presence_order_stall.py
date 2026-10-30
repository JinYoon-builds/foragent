"""Acceptance test for presence-order-stall. Locked by Foragent; do not edit."""

from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from foragent import orch_inbox, presence
from foragent.state import StateStore


NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)
BASE = "🐿 0 lanes working · ✋ 0 · 0 merged today"


@pytest.fixture
def stall_case(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    root = tmp_path / "project"
    root.mkdir()
    clock = [NOW]
    store = StateStore.for_repo("offline/order-stall", now=lambda: clock[0])

    def configure(language="en", enabled=True):
        (root / ".foragent.yml").write_text(
            f"repo: offline/order-stall\nlanguage: {language}\n"
            f"presence:\n  statusline: {str(enabled).lower()}\n", encoding="utf-8")

    def request(age, source="planning", status="processing"):
        clock[0] = NOW - timedelta(days=2)
        row = orch_inbox.add_request(store, source, "opaque request content")
        clock[0] = NOW - age
        if status != "new":
            orch_inbox.set_request_status(store, row["id"], status)
        clock[0] = NOW
        return row["id"]

    def rewrite(request_id, **changes):
        # Model legacy rows and damaged timestamp fields without changing the writer.
        path = store.root / "inbox" / "requests" / f"{request_id}.json"
        row = json.loads(path.read_text(encoding="utf-8"))
        row.update(changes)
        path.write_text(json.dumps(row), encoding="utf-8")

    def forbidden(*args, **kwargs):
        pytest.fail("The planning statusline must read local facts without network or child processes")

    import socket
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    configure()
    return SimpleNamespace(root=root, store=store, clock=clock, request=request,
                           configure=configure, rewrite=rewrite,
                           segment=lambda: presence.statusline_segment(root, clock[0]))


@pytest.mark.parametrize("language,base,fact", [
    ("en", BASE, "Stuck requests: 4 (over 8 h) · fa orch inbox"),
    ("ko", "🐿 레인 0 일하는 중 · ✋ 0 · 오늘 머지 0", "요청 4개가 8시간 넘게 멈춰 있어요 · fa orch inbox"),
])
def test_presence_order_stall_counts_only_old_processing_planning_requests(stall_case, language, base, fact):
    """Legacy test name retained: every source counts, only status and age filter rows."""
    case = stall_case
    case.configure(language)
    assert case.segment() == base
    case.request(timedelta(hours=3, minutes=59))
    case.request(timedelta(hours=1, minutes=5))
    case.request(timedelta(hours=8), source="gate")
    case.request(timedelta(hours=8), source="architect")
    case.request(timedelta(hours=8), status="new")
    case.request(timedelta(hours=8), status="done")
    case.request(timedelta(minutes=59))
    segment = case.segment()
    assert segment is not None and segment.startswith(base + " · ")
    assert segment.endswith(fact), "Count qualifying requests and floor the oldest transition's age to hours"
    assert "\n" not in segment


def test_presence_order_stall_requires_strictly_more_than_sixty_minutes(stall_case):
    case = stall_case
    case.request(timedelta(hours=1, seconds=1))
    assert case.segment() == BASE + " · Stuck requests: 1 (over 1 h) · fa orch inbox"
    case.clock[0] = NOW - timedelta(seconds=1)
    assert case.segment() == BASE, "Exactly sixty minutes is not older than sixty minutes"
    case.clock[0] = NOW - timedelta(seconds=2)
    assert case.segment() == BASE, "Do not show stalls or minute ages below the threshold"


@pytest.mark.parametrize("kind", ["question", "merge", "order"])
def test_presence_order_stall_is_hidden_while_any_human_question_is_open(stall_case, kind):
    case = stall_case
    case.request(timedelta(hours=3))
    assert case.segment() == BASE + " · Stuck requests: 1 (over 3 h) · fa orch inbox"
    # Legacy test name retained: open questions must never hide the stall fact.
    question = orch_inbox.ask(case.store, "Choose", ["add", "later"], 1, kind=kind)
    assert case.segment() == BASE + " · Stuck requests: 1 (over 3 h) · fa orch inbox"
    orch_inbox.answer(case.store, question["id"], "add")
    assert case.segment() == BASE + " · Stuck requests: 1 (over 3 h) · fa orch inbox", "Closing a question does not change the stall count"


def test_presence_order_stall_uses_status_transition_not_repeated_processing_notes(stall_case):
    case = stall_case
    request_id = case.request(timedelta(hours=3))
    case.clock[0] = NOW - timedelta(minutes=5)
    orch_inbox.set_request_status(case.store, request_id, "processing", "A repeated status note")
    case.clock[0] = NOW
    assert case.segment() == BASE + " · Stuck requests: 1 (over 3 h) · fa orch inbox"
    case.clock[0] = NOW - timedelta(minutes=5)
    orch_inbox.set_request_status(case.store, request_id, "done")
    orch_inbox.set_request_status(case.store, request_id, "processing")
    case.clock[0] = NOW
    assert case.segment() == BASE, "An actual transition back into processing starts a new age"
    case.clock[0] = NOW + timedelta(hours=1)
    assert case.segment() == BASE + " · Stuck requests: 1 (over 1 h) · fa orch inbox"


def test_presence_order_stall_keeps_the_statusline_setting_effective(stall_case):
    case = stall_case
    case.request(timedelta(hours=3))
    assert case.segment() == BASE + " · Stuck requests: 1 (over 3 h) · fa orch inbox"
    case.configure(enabled=False)
    assert case.segment() is None


def test_presence_order_stall_formats_the_fact_through_i18n_text(stall_case, monkeypatch):
    from foragent import i18n

    calls = []
    original = i18n.text

    def record(key, language="en", **values):
        result = original(key, language, **values)
        calls.append((language, result))
        return result

    monkeypatch.setattr(i18n, "text", record)
    stall_case.request(timedelta(hours=3))
    assert stall_case.segment() == BASE + " · Stuck requests: 1 (over 3 h) · fa orch inbox"
    assert ("en", "Stuck requests: 1 (over 3 h) · fa orch inbox") in calls, "Format the appended fact through text()"


@pytest.mark.parametrize("role", ["lane", "orchestrator", "architect", ""])
def test_presence_order_stall_is_only_in_a_session_without_foragent_role(stall_case, monkeypatch, role):
    case = stall_case
    case.request(timedelta(hours=3), source="gate")
    fact = " · Stuck requests: 1 (over 3 h) · fa orch inbox"
    assert case.segment() == BASE + fact
    monkeypatch.setenv("FORAGENT_ROLE", role)
    assert case.segment() == BASE, "Even an empty FORAGENT_ROLE excludes a planning session"
    monkeypatch.delenv("FORAGENT_ROLE")
    assert case.segment() == BASE + fact


def test_presence_order_stall_is_absent_inside_gate_worktrees_including_symlinks(stall_case, tmp_path):
    case = stall_case
    case.request(timedelta(hours=3))
    fact = " · Stuck requests: 1 (over 3 h) · fa orch inbox"
    assert case.segment() == BASE + fact
    worktree = case.store.root / "worktrees" / "issue-1"
    worktree.mkdir(parents=True)
    (worktree / ".foragent.yml").write_text((case.root / ".foragent.yml").read_text())
    child = worktree / "nested"
    child.mkdir()
    alias = tmp_path / "worktree-link"
    alias.symlink_to(worktree, target_is_directory=True)
    for cwd in (worktree, child, alias):
        assert presence.statusline_segment(cwd, NOW) == BASE
    assert case.segment() == BASE + fact, "The planning session still sees the same requests"


@pytest.mark.parametrize("timestamp", ["broken", "2026-10-30T08:00:00", None, 17])
@pytest.mark.parametrize("legacy", [False, True], ids=["transition", "creation"])
def test_presence_order_stall_skips_only_the_bad_timestamp_and_keeps_existing_counts(
    stall_case, timestamp, legacy,
):
    case = stall_case
    case.request(timedelta(hours=3), source="gate")
    bad = case.request(timedelta(hours=9), source="architect")
    if legacy:
        case.rewrite(bad, notes=[], created_at=timestamp)
    else:
        case.rewrite(bad, notes=[{"status": "processing", "at": timestamp}])
    case.clock[0] = NOW - timedelta(minutes=30)
    case.store.start_run(7, branch="foragent/issue-7", worktree=case.store.worktree_path(7), lane="main")
    case.store.record_merge(7, NOW - timedelta(minutes=20))
    question = orch_inbox.ask(case.store, "Choose", ["yes", "later"], 1, issues=[88])
    case.clock[0] = NOW
    base = "🐿 1 lanes working · ✋ 1 · 1 merged today"
    assert case.segment() == base + " · Stuck requests: 1 (over 3 h) · fa orch inbox"
    orch_inbox.set_request_status(case.store, "r1", "done")
    assert case.segment() == base, "Only the malformed row remains: keep all original counts"
    orch_inbox.answer(case.store, question["id"], "yes")


def test_presence_order_stall_uses_creation_for_requests_without_status_changes(stall_case):
    case = stall_case
    old = case.request(timedelta(hours=3), source="architect")
    young = case.request(timedelta(hours=3), source="gate")
    # Recent updated_at must not hide a legacy stall, or make a young legacy row old.
    case.rewrite(old, notes=[], created_at=(NOW - timedelta(hours=5, minutes=40)).isoformat(),
                 updated_at=NOW.isoformat())
    case.rewrite(young, notes=[], created_at=(NOW - timedelta(minutes=20)).isoformat(),
                 updated_at=(NOW - timedelta(days=2)).isoformat())
    assert case.segment() == BASE + " · Stuck requests: 1 (over 5 h) · fa orch inbox"
    orch_inbox.set_request_status(case.store, old, "done", "#101")
    assert case.segment() == BASE, "Completing the originating request clears the stall"


@pytest.mark.parametrize("language,base,fact", [
    ("ko", "🐿 레인 0 일하는 중 · ✋ 0 · 오늘 머지 0",
     "요청 1개가 3시간 넘게 멈춰 있어요 · fa orch inbox"),
    ("ko-KR", "🐿 레인 0 일하는 중 · ✋ 0 · 오늘 머지 0",
     "요청 1개가 3시간 넘게 멈춰 있어요 · fa orch inbox"),
    ("fr", BASE, "Stuck requests: 1 (over 3 h) · fa orch inbox"),
])
def test_presence_order_stall_uses_singular_safe_copy_and_normal_language_fallback(
    stall_case, language, base, fact,
):
    case = stall_case
    case.configure(language)
    case.request(timedelta(hours=3))
    assert case.segment() == base + " · " + fact
