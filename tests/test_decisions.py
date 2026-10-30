"""Decision links and completion facts kept through the local issue snapshot (#1025)."""

from datetime import UTC, datetime, timedelta

import pytest

from foragent import decisions
from foragent.state import StateStore
from foragent.tracker import Issue

NOW = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
LEDGER = "".join(f"### D{n}. Decision {n}\n" for n in (7, 74, 79))


def store(tmp_path, now=NOW):
    return StateStore(tmp_path / "state", now=lambda: now)


def test_refresh_records_completion_reason_and_failed_refresh_keeps_observation(tmp_path):
    state = store(tmp_path)
    state.refresh_local_issues([Issue(1, "Done", "", frozenset(), "closed", state_reason="completed"),
                                Issue(2, "Cancelled", "", frozenset(), "closed", state_reason="not_planned")],
                               [], {}, NOW)
    rows = {row["number"]: row for row in state.local_issues()}
    assert rows[1]["state_reason"] == "completed" and rows[2]["state_reason"] == "not_planned"
    assert rows[1]["observed_at"] == NOW.isoformat()
    state.record_issues_refresh_failure("network down", NOW + timedelta(hours=1))
    assert {row["number"]: row["observed_at"] for row in state.local_issues()} == {
        1: NOW.isoformat(), 2: NOW.isoformat()}


def test_reopened_issue_listing_forgets_the_old_completion_reason(tmp_path):
    state = store(tmp_path)
    state.refresh_local_issues([Issue(1, "T", "", frozenset(), "closed", state_reason="completed")], [], {}, NOW)
    later = NOW + timedelta(minutes=10)
    state.refresh_local_issues([Issue(1, "T", "", frozenset(), "open")], [], {}, later)
    row = state.local_issues()[0]
    assert row["state"] == "open" and row["state_reason"] is None and row["observed_at"] == later.isoformat()


def test_single_issue_read_records_reason_and_its_observation_time(tmp_path):
    read_at = NOW + timedelta(minutes=3)
    state = store(tmp_path, read_at)
    state.record_issue(Issue(4, "T", "", frozenset(), "closed", state_reason="completed"), {})
    row = state.local_issues()[0]
    assert row["state_reason"] == "completed" and row["observed_at"] == read_at.isoformat()


def test_parent_map_and_spec_reference_link_legacy_children_exactly(tmp_path):
    specs = tmp_path / "docs/specs"
    specs.mkdir(parents=True)
    (specs / "old.md").write_text("D74-1 twice: D74. Not D740 or XD7.\n")
    (specs / "ref.md").write_text("D79\n")
    rows = [
        {"number": 1, "body": '<!-- foragent:plan id=old map={"a": 2} -->', "state": "open"},
        {"number": 2, "body": "no marker", "state": "closed", "state_reason": "completed",
         "observed_at": NOW.isoformat()},
        {"number": 3, "body": "See docs/specs/ref.md", "state": "closed", "state_reason": "completed",
         "merged": True},
        {"number": 4, "body": "D7 mentioned in prose only", "state": "open"},
    ]
    result = {row["id"]: row for row in decisions.view(tmp_path, rows, LEDGER)}
    assert result["D74"] == {"id": "D74", "issues": [2], "state": "done", "observed_at": NOW.isoformat()}
    assert result["D79"]["issues"] == [3] and result["D79"]["observed_at"] is None
    assert result["D7"]["issues"] == [] and result["D7"]["state"] == "decided, nothing ordered"


def test_closed_without_reason_or_merge_and_merged_cancellation_are_not_done(tmp_path):
    rows = [{"number": 1, "body": decisions.marker(["D7"]), "state": "closed", "state_reason": None},
            {"number": 2, "body": decisions.marker(["D74"]), "state": "closed",
             "state_reason": "not_planned", "merged": True}]
    result = {row["id"]: row["state"] for row in decisions.view(tmp_path, rows, LEDGER)}
    assert result == {"D7": "in progress", "D74": "in progress", "D79": "decided, nothing ordered"}


def test_marker_is_canonical_and_omission_keeps_the_previous_marker():
    assert decisions.marker(["D79", "D7", "D79"]) == '<!-- foragent:decisions ["D7", "D79"] -->'
    previous = "Body\n" + decisions.marker([]) + "\n"
    assert decisions.with_marker("New\n", None, previous) == "New\n<!-- foragent:decisions [] -->\n"
    assert decisions.with_marker("New\n", None, "Body\n") == "New\n"
    assert decisions.explicit("New\n<!-- foragent:decisions [] -->\n") == []
    assert decisions.explicit("New\n") is None


def test_mapped_child_missing_from_the_snapshot_keeps_its_link_and_blocks_done(tmp_path):
    specs = tmp_path / "docs/specs"
    specs.mkdir(parents=True)
    (specs / "old.md").write_text("D74\n")
    rows = [
        {"number": 1, "body": '<!-- foragent:plan id=old map={"a": 2, "b": 3} -->', "state": "open"},
        {"number": 2, "body": "no marker", "state": "closed", "state_reason": "completed",
         "observed_at": NOW.isoformat()},
    ]
    result = {row["id"]: row for row in decisions.view(tmp_path, rows, LEDGER)}
    assert result["D74"] == {"id": "D74", "issues": [2, 3], "state": "in progress", "observed_at": None}


def test_edit_only_and_mixed_order_records_link_every_published_issue(tmp_path):
    specs = tmp_path / "docs/specs"
    specs.mkdir(parents=True)
    (specs / "edits.md").write_text("D7\n")
    (specs / "mixed.md").write_text("D79\n")
    rows = [
        {"number": 1, "body": '<!-- foragent:plan id=edits map={} acceptance={"5": {}} -->'},
        {"number": 2, "body": '<!-- foragent:plan id=mixed map={"a": 3} acceptance={"3": {}, "6": {}} -->'},
        *({"number": n, "body": "edited body"} for n in (3, 5, 6)),
    ]
    result = {row["id"]: row["issues"] for row in decisions.view(tmp_path, rows, LEDGER)}
    assert result == {"D7": [5], "D74": [], "D79": [3, 6]}


@pytest.mark.parametrize("text, linked", [
    ("D74", True), ("D74-2", True), ("(D74).", True), ("D74x", False), ("D74_1", False),
    ("D740", False), ("XD74", False), ("_D74", False),
])
def test_citations_match_whole_identifiers_only(tmp_path, text, linked):
    (tmp_path / "docs/specs").mkdir(parents=True)
    (tmp_path / "docs/specs/old.md").write_text(text + "\n")
    rows = [{"number": 2, "body": "<!-- foragent:plan-child id=old key=a -->"}]
    result = {row["id"]: row["issues"] for row in decisions.view(tmp_path, rows, LEDGER)}
    assert result["D74"] == ([2] if linked else [])


@pytest.mark.parametrize("body, linked", [
    ("docs/specs/old.md", True), ("https://x/blob/main/docs/specs/old.md", True),
    ("mydocs/specs/old.md", False), ("docs/specs/old.mdx", False),
])
def test_spec_references_match_whole_paths_only(tmp_path, body, linked):
    (tmp_path / "docs/specs").mkdir(parents=True)
    (tmp_path / "docs/specs/old.md").write_text("D74\n")
    result = {row["id"]: row["issues"] for row in decisions.view(tmp_path, [{"number": 2, "body": body}], LEDGER)}
    assert result["D74"] == ([2] if linked else [])


def test_undelivered_close_is_local_intent_until_delivery(tmp_path):
    state = store(tmp_path)
    marker = decisions.marker(["D79"])
    state.refresh_local_issues([Issue(9, "T", marker, frozenset(), "open")], [], {}, NOW)
    state.update_local_issue(9, merged=True)
    entry = state.queue_github_write(9, "close_issue", body=None)
    assert state.local_issues()[0]["state"] == "closed", "Local intent shows the queued close"

    def d79():
        return next(row for row in decisions.view(tmp_path, state.confirmed_local_issues(), LEDGER)
                    if row["id"] == "D79")

    assert d79()["state"] == "in progress", "GitHub has not closed it yet"
    state.deliver_github_write(entry)
    assert d79()["state"] == "done", "Delivery means GitHub accepted the close"
