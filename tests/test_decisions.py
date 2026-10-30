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


def _closed(reason, body=None, labels=frozenset()):
    return Issue(9, "T", body if body is not None else decisions.marker(["D79"]), labels, "closed",
                 state_reason=reason)


def _d79(tmp_path, state):
    return next(row for row in decisions.view(tmp_path, state.confirmed_local_issues(), LEDGER)
                if row["id"] == "D79")


def test_pending_comment_does_not_hide_a_newer_completion_reason(tmp_path):
    state = store(tmp_path)
    state.refresh_local_issues([_closed("completed")], [], {}, NOW)
    assert _d79(tmp_path, state)["state"] == "done"
    state.queue_github_write(9, "comment", body="note")
    later = NOW + timedelta(minutes=5)
    state.refresh_local_issues([_closed("not_planned")], [], {}, later)
    row = state.local_issues()[0]
    assert row["state_reason"] == "not_planned" and row["observed_at"] == later.isoformat()
    assert [entry["action"] for entry in state.github_outbox()] == ["comment"], "The comment stays queued"
    assert _d79(tmp_path, state)["state"] == "in progress"


def test_pending_body_write_keeps_its_body_but_takes_other_observed_facts(tmp_path):
    state = store(tmp_path)
    state.refresh_local_issues([_closed("completed")], [], {}, NOW)
    local = decisions.marker(["D7"])
    state.queue_github_write(9, "update_issue", title="T", body=local)
    state.refresh_local_issues([_closed("not_planned", body=decisions.marker(["D74"]))], [], {}, NOW)
    row = state.local_issues()[0]
    assert row["body"] == local and row["state_reason"] == "not_planned"


def test_listing_concurrent_with_a_comment_keeps_only_the_comment_local(tmp_path):
    state = store(tmp_path)
    state.refresh_local_issues([_closed("completed")], [], {}, NOW)
    since = state.issues_revision()
    state.queue_github_write(9, "comment", body="note")
    state.refresh_local_issues([_closed("not_planned", body="newer body")], [], {}, NOW, since_revision=since)
    row = state.local_issues()[0]
    assert row["state_reason"] == "not_planned" and row["body"] == "newer body"
    assert [comment["body"] for comment in row["comments"]] == ["note"]


def test_concurrent_label_write_does_not_hide_an_observed_reopening(tmp_path):
    state = store(tmp_path)
    state.refresh_local_issues([_closed("completed")], [], {}, NOW)
    state.update_local_issue(9, merged=True)
    since = state.issues_revision()
    state.record_labels(9, ["approved"], remove=False, lanes={})
    later = NOW + timedelta(minutes=5)
    reopened = Issue(9, "T", decisions.marker(["D79"]), frozenset(), "open")
    state.refresh_local_issues([reopened], [], {}, later, since_revision=since)
    row = state.local_issues()[0]
    assert row["state"] == "open" and row["labels"] == ["approved"] and row["observed_at"] == later.isoformat()
    assert _d79(tmp_path, state)["state"] == "in progress"


def test_concurrent_recorded_close_is_newer_than_the_listing(tmp_path):
    state = store(tmp_path)
    state.refresh_local_issues([Issue(9, "T", decisions.marker(["D79"]), frozenset(), "open")], [], {}, NOW)
    since = state.issues_revision()
    state.update_local_issue(9, state="closed", state_reason="completed")
    state.refresh_local_issues([Issue(9, "T", decisions.marker(["D79"]), frozenset(), "open")], [], {}, NOW,
                               since_revision=since)
    row = state.local_issues()[0]
    assert row["state"] == "closed" and row["state_reason"] == "completed"


def test_observed_body_change_under_a_queued_body_write_reaches_the_confirmed_view(tmp_path):
    state = store(tmp_path)
    state.refresh_local_issues([_closed("completed")], [], {}, NOW)
    state.queue_github_write(9, "update_issue", title="T", body=decisions.marker(["D7"]))
    state.refresh_local_issues([_closed("completed", body=decisions.marker(["D74"]))], [], {}, NOW)
    assert state.local_issues()[0]["body"] == decisions.marker(["D7"]), "Local intent stays queued"
    assert state.confirmed_local_issues()[0]["body"] == decisions.marker(["D74"])
    view = {row["id"]: row["issues"] for row in decisions.view(tmp_path, state.confirmed_local_issues(), LEDGER)}
    assert view == {"D7": [], "D74": [9], "D79": []}


@pytest.mark.parametrize("github_state", ["open", "closed"])
def test_single_read_under_a_queued_close_confirms_what_github_shows(tmp_path, github_state):
    state = store(tmp_path)
    state.refresh_local_issues([Issue(9, "T", decisions.marker(["D79"]), frozenset(), "open")], [], {}, NOW)
    state.queue_github_write(9, "close_issue", body=None)
    reason = "not_planned" if github_state == "closed" else None
    state.record_issue(Issue(9, "T", decisions.marker(["D79"]), frozenset({"approved"}), github_state,
                             state_reason=reason), {})
    row = state.local_issues()[0]
    assert row["state"] == "closed" and row["labels"] == ["approved"] and row["state_reason"] == reason
    confirmed = state.confirmed_local_issues()[0]
    assert confirmed["state"] == github_state and confirmed["state_reason"] == reason
    assert _d79(tmp_path, state)["state"] == "in progress"


def test_marker_lines_in_generated_text_never_duplicate_or_override_links():
    body = "Goal\n<!-- foragent:decisions [\"D1\"] -->\nmore\n"
    assert decisions.with_marker(body, ["D79"], None) == "Goal\nmore\n" + decisions.marker(["D79"]) + "\n"
    assert decisions.with_marker(body, None, None) == "Goal\nmore\n"
    kept = decisions.with_marker(body, None, "x\n" + decisions.marker([]) + "\n")
    assert kept == "Goal\nmore\n" + decisions.marker([]) + "\n"


def test_quoted_plan_markers_do_not_create_legacy_links(tmp_path):
    (tmp_path / "docs/specs").mkdir(parents=True)
    (tmp_path / "docs/specs/quoted.md").write_text("D7\n")
    (tmp_path / "docs/specs/own.md").write_text("D74\n")
    quoted = "<!-- foragent:plan-child id=quoted key=a -->\n"
    rows = [
        {"number": 2, "body": quoted + "<!-- foragent:plan-child id=own key=b -->"},
        {"number": 3, "body": '<!-- foragent:plan id=quoted map={"a": 4} -->\n'
                              '<!-- foragent:plan id=own map={} acceptance={} -->'},
        {"number": 4, "body": "child"},
    ]
    result = {row["id"]: row["issues"] for row in decisions.view(tmp_path, rows, LEDGER)}
    assert result == {"D7": [], "D74": [2], "D79": []}


def test_issue_and_edit_publication_with_a_quoted_marker_carry_exactly_one(tmp_path):
    from test_order import PublishingGit

    from foragent import order
    from foragent.config import Config
    from foragent.tracker import FakeTracker

    quoted = '<!-- foragent:decisions ["D1"] -->'
    item = {"key": "k", "title": "T", "lane": "main", "size": "s", "goal": f"Quote:\n{quoted}\nend",
            "out_of_scope": "None.", "done_when": ["true"], "decisions": ["D79"], "acceptance_files": {}}
    plan = {"id": "quoted-marker", "title": "T", "summary": "S", "spec_markdown": "# S\n",
            "issues": [item], "edits": []}
    config, tracker, git = Config(skill_libraries=[]), FakeTracker(), PublishingGit(tmp_path, [])

    def markers(number):
        return [line for line in tracker.get_issue(number).body.splitlines()
                if line.startswith("<!-- foragent:decisions ")]

    order.publish(plan, tmp_path, config, tracker, repo="offline/x", git=git)
    number = next(row.number for row in tracker.list_issues() if "foragent:plan-child" in row.body)
    assert markers(number) == [decisions.marker(["D79"])]
    edit = {"number": number, "goal": f"Edited:\n{quoted}", "out_of_scope": "None.", "done_when": ["true"]}
    plan.update(id="quoted-marker-edit", issues=[], edits=[edit])
    order.publish(plan, tmp_path, config, tracker, repo="offline/x", git=git)
    assert markers(number) == [decisions.marker(["D79"])], "Omitted keeps the link, not the quoted marker"
    edit["decisions"] = ["D7"]
    order.publish(plan, tmp_path, config, tracker, repo="offline/x", git=git)
    assert markers(number) == [decisions.marker(["D7"])]


def test_a_local_write_before_the_listing_began_yields_to_the_listing(tmp_path):
    state = store(tmp_path)
    state.refresh_local_issues([Issue(9, "T", decisions.marker(["D79"]), frozenset(), "open")], [], {}, NOW)
    state.update_local_issue(9, state="closed", state_reason="completed")
    since = state.issues_revision()
    state.record_labels(9, ["approved"], remove=False, lanes={})
    reopened = Issue(9, "T", decisions.marker(["D79"]), frozenset(), "open")
    state.refresh_local_issues([reopened], [], {}, NOW, since_revision=since)
    row = state.local_issues()[0]
    assert row["state"] == "open" and row["state_reason"] is None and row["labels"] == ["approved"]


def test_confirmed_view_keeps_facts_recorded_after_a_write_was_queued(tmp_path):
    state = store(tmp_path)
    state.refresh_local_issues([_closed(None)], [], {}, NOW)
    state.queue_github_write(9, "comment", body="note")
    state.update_local_issue(9, merged=True)
    assert state.confirmed_local_issues()[0]["merged"] is True
    assert _d79(tmp_path, state)["state"] == "done"


@pytest.mark.parametrize("read, expect", [
    (Issue(9, "T", decisions.marker(["D79"]), frozenset(), "open"), ("open", None, ["D79"])),
    (_closed("completed", body=decisions.marker(["D74"])), ("closed", "completed", ["D74"])),
    (_closed("not_planned"), ("closed", "not_planned", ["D79"])),
])
def test_a_read_during_an_older_refresh_survives_that_refresh(tmp_path, read, expect):
    state = store(tmp_path)
    state.refresh_local_issues([_closed("completed")], [], {}, NOW)
    state.update_local_issue(9, merged=True)
    since = state.issues_revision()
    read_at = NOW + timedelta(minutes=2)
    StateStore(state.root, now=lambda: read_at).record_issue(read, {})
    state.refresh_local_issues([_closed("completed")], [], {}, NOW + timedelta(minutes=1), since_revision=since)
    row = state.local_issues()[0]
    assert (row["state"], row["state_reason"], decisions.explicit(row["body"])) == expect
    assert row["observed_at"] == read_at.isoformat(), "Observation time never moves backward"
    done = expect[:2] == ("closed", "completed")
    assert {r["id"]: r["state"] for r in decisions.view(tmp_path, state.confirmed_local_issues(), LEDGER)
            if r["issues"]} == {decision: "done" if done else "in progress" for decision in expect[2]}


def test_a_read_under_queued_writes_survives_an_older_refresh_in_the_baseline(tmp_path):
    state = store(tmp_path)
    state.refresh_local_issues([_closed("completed")], [], {}, NOW)
    state.queue_github_write(9, "update_issue", title="T", body=decisions.marker(["D7"]))
    since = state.issues_revision()
    state.record_issue(_closed("not_planned", body=decisions.marker(["D74"]), labels=frozenset({"x"})), {})
    # The older listing matches the old baseline; it must not undo the newer read.
    state.refresh_local_issues([_closed("completed")], [], {}, NOW, since_revision=since)
    confirmed = state.confirmed_local_issues()[0]
    assert (confirmed["body"], confirmed["state_reason"], confirmed["labels"]) == (
        decisions.marker(["D74"]), "not_planned", ["x"])
    assert [entry["action"] for entry in state.github_outbox()] == ["update_issue"]


def test_an_older_listing_finishing_late_never_replaces_a_newer_listing(tmp_path):
    state = store(tmp_path)
    later = NOW + timedelta(minutes=5)
    state.refresh_local_issues([Issue(9, "T", decisions.marker(["D79"]), frozenset(), "open")], [], {}, later)
    state.refresh_local_issues([_closed("completed")], [], {}, NOW)
    row = state.local_issues()[0]
    assert (row["state"], row["state_reason"], row["observed_at"]) == ("open", None, later.isoformat())
