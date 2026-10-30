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


# Observation ordering (#1025 rule, 2026-10-31): one writer applies an observation only
# when it is not older than the stored one; local writes are an overlay on top of it.

def minutes(n):
    return NOW + timedelta(minutes=n)


class Clock:
    def __init__(self):
        self.at = NOW

    def store(self, tmp_path):
        return StateStore(tmp_path / "state", now=lambda: self.at)


def issue(state="open", reason=None, body=None, labels=()):
    return Issue(9, "T", body if body is not None else decisions.marker(["D79"]), frozenset(labels), state,
                 state_reason=reason)


def d79(tmp_path, state):
    return next(row for row in decisions.view(tmp_path, state.confirmed_local_issues(), LEDGER)
                if row["id"] == "D79")


def test_undelivered_close_is_local_intent_until_delivery(tmp_path):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue()], [], {}, NOW)
    state.update_local_issue(9, merged=True)
    clock.at = minutes(1)
    entry = state.queue_github_write(9, "close_issue", body=None)
    assert state.local_issues()[0]["state"] == "closed", "Local intent shows the queued close"
    assert d79(tmp_path, state)["state"] == "in progress", "GitHub has not closed it yet"
    clock.at = minutes(2)
    state.deliver_github_write(entry)
    assert d79(tmp_path, state)["state"] == "done", "Delivery means GitHub accepted the close"
    state.refresh_local_issues([issue("closed", "completed")], [], {}, minutes(3))
    row = state.local_issues()[0]
    assert "unconfirmed" not in row and row["github"]["state_reason"] == "completed"


def test_pending_comment_does_not_hide_a_newer_completion_reason(tmp_path):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue("closed", "completed")], [], {}, NOW)
    assert d79(tmp_path, state)["state"] == "done"
    clock.at = minutes(1)
    state.queue_github_write(9, "comment", body="note")
    # A new close reason is a person's change, like a label or state change: it
    # contradicts the issue's queued writes, comments included, as today.
    assert state.refresh_local_issues([issue("closed", "not_planned")], [], {}, minutes(5)) == [9]
    row = state.local_issues()[0]
    assert row["state_reason"] == "not_planned" and row["observed_at"] == minutes(5).isoformat()
    assert state.github_outbox() == [] and row["comments"] == []
    assert d79(tmp_path, state)["state"] == "in progress"


@pytest.mark.parametrize("action, payload, seen, field, value", [
    ("update_issue", {"title": "T", "body": decisions.marker(["D7"])},
     issue(body=decisions.marker(["D74"])), "body", decisions.marker(["D74"])),
    ("add_labels", {"labels": ["x"]}, issue(labels=["y"]), "labels", ["y"]),
    ("close_issue", {"body": None}, issue("closed", "not_planned"), "state_reason", "not_planned"),
])
def test_a_newer_contradicting_observation_drops_the_pending_write(tmp_path, action, payload, seen, field, value):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue()], [], {}, NOW)
    clock.at = minutes(1)
    state.queue_github_write(9, action, **payload)
    assert state.refresh_local_issues([seen], [], {}, minutes(2)) == [9]
    assert state.github_outbox() == []
    row = state.local_issues()[0]
    assert row[field] == value and state.confirmed_local_issues()[0][field] == value


@pytest.mark.parametrize("action, payload, field, local", [
    ("update_issue", {"title": "T", "body": decisions.marker(["D7"])}, "body", decisions.marker(["D7"])),
    ("add_labels", {"labels": ["x"]}, "labels", ["x"]),
    ("close_issue", {"body": None}, "state", "closed"),
])
def test_a_pending_write_stays_over_observations_that_predate_it_or_show_no_change(
        tmp_path, action, payload, field, local):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue()], [], {}, NOW)
    clock.at = minutes(5)
    state.queue_github_write(9, action, **payload)
    # A listing that began before the write cannot judge it, even if someone changed the body.
    assert state.refresh_local_issues([issue(body="edited")], [], {}, minutes(3)) == []
    row = state.local_issues()[0]
    assert row[field] == local and len(state.github_outbox()) == 1
    assert state.confirmed_local_issues()[0]["body"] == "edited"
    # A later listing that shows GitHub unchanged means the write is still undelivered.
    assert state.refresh_local_issues([issue(body="edited")], [], {}, minutes(6)) == []
    assert state.local_issues()[0][field] == local and len(state.github_outbox()) == 1


def test_a_late_older_refresh_after_a_newer_one_applies_nothing(tmp_path):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue()], [], {}, NOW)
    clock.at = minutes(1)
    state.queue_github_write(9, "add_labels", labels=["x"])
    state.refresh_local_issues([issue(body=decisions.marker(["D74"]))], [], {}, minutes(3))
    before = (state.local_issues(), state.github_outbox(), state.confirmed_local_issues())
    clock.at = minutes(10)
    assert state.refresh_local_issues([issue(body=decisions.marker(["D79"]), labels=["z"])], [], {},
                                      minutes(2)) == []
    assert (state.local_issues(), state.github_outbox(), state.confirmed_local_issues()) == before
    assert decisions.explicit(state.confirmed_local_issues()[0]["body"]) == ["D74"]
    assert d79(tmp_path, state)["issues"] == [] and state.issues_observation()["as_of"] == minutes(3).isoformat()


def test_a_late_older_refresh_after_a_newer_one_keeps_a_dropped_write_dropped(tmp_path):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue()], [], {}, NOW)
    clock.at = minutes(1)
    state.queue_github_write(9, "update_issue", title="T", body=decisions.marker(["D7"]))
    state.refresh_local_issues([issue(body=decisions.marker(["D74"]))], [], {}, minutes(3))
    assert state.github_outbox() == []
    state.refresh_local_issues([issue()], [], {}, minutes(2))
    row = state.confirmed_local_issues()[0]
    assert decisions.explicit(row["body"]) == ["D74"] and row["observed_at"] == minutes(3).isoformat()


def test_an_older_listing_never_drops_writes_for_an_issue_it_cannot_have_seen(tmp_path):
    clock = Clock()
    state = clock.store(tmp_path)
    clock.at = minutes(5)
    state.queue_github_write(9, "comment", body="note")
    assert state.refresh_local_issues([], [], {}, minutes(4)) == []
    assert len(state.github_outbox()) == 1
    assert state.refresh_local_issues([], [], {}, minutes(6)) == [9]


def test_a_delivered_write_overlays_listings_that_began_before_its_delivery(tmp_path):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue()], [], {}, NOW)
    clock.at = minutes(1)
    entry = state.queue_github_write(9, "add_labels", labels=["approved"])
    clock.at = minutes(3)
    state.deliver_github_write(entry)
    state.refresh_local_issues([issue()], [], {}, minutes(2))
    assert state.local_issues()[0]["labels"] == ["approved"]
    state.refresh_local_issues([issue()], [], {}, minutes(4))
    assert state.local_issues()[0]["labels"] == [], "A later listing shows what GitHub has"


def test_a_direct_write_result_never_hides_an_observed_reopening(tmp_path):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue("closed", "completed")], [], {}, NOW)
    state.update_local_issue(9, merged=True)
    since = state.issues_revision()
    clock.at = minutes(2)
    state.record_labels(9, ["approved"], remove=False, lanes={})
    state.refresh_local_issues([issue()], [], {}, minutes(1), since_revision=since)
    row = state.local_issues()[0]
    assert (row["state"], row["labels"], row["observed_at"]) == ("open", ["approved"], minutes(1).isoformat())
    assert d79(tmp_path, state)["state"] == "in progress"
    state.refresh_local_issues([issue()], [], {}, minutes(3))
    assert state.local_issues()[0]["labels"] == []


def test_a_recorded_close_overlays_an_older_listing_until_a_later_one(tmp_path):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue()], [], {}, NOW)
    clock.at = minutes(2)
    state.update_local_issue(9, state="closed", state_reason="completed")
    state.refresh_local_issues([issue()], [], {}, minutes(1))
    assert state.local_issues()[0]["state"] == "closed" and d79(tmp_path, state)["state"] == "done"
    state.refresh_local_issues([issue()], [], {}, minutes(3))
    assert state.local_issues()[0]["state"] == "open" and d79(tmp_path, state)["state"] == "in progress"


@pytest.mark.parametrize("seen, expect", [
    (issue(), ("open", None, ["D79"])),
    (issue("closed", "completed", body=decisions.marker(["D74"])), ("closed", "completed", ["D74"])),
    (issue("closed", "not_planned"), ("closed", "not_planned", ["D79"])),
])
def test_a_read_during_an_older_refresh_survives_that_refresh(tmp_path, seen, expect):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue("closed", "completed")], [], {}, NOW)
    state.update_local_issue(9, merged=True)
    clock.at = minutes(2)
    state.record_issue(seen, {})
    state.refresh_local_issues([issue("closed", "completed")], [], {}, minutes(1))
    row = state.local_issues()[0]
    assert (row["state"], row["state_reason"], decisions.explicit(row["body"])) == expect
    assert row["observed_at"] == minutes(2).isoformat(), "Observation time never moves backward"


@pytest.mark.parametrize("github_state", ["open", "closed"])
def test_single_read_under_a_queued_close_confirms_what_github_shows(tmp_path, github_state):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue()], [], {}, NOW)
    clock.at = minutes(1)
    state.queue_github_write(9, "close_issue", body=None)
    clock.at = minutes(2)
    reason = "not_planned" if github_state == "closed" else None
    state.record_issue(issue(github_state, reason), {})
    assert state.local_issues()[0]["state"] == "closed"
    assert len(state.github_outbox()) == (1 if github_state == "open" else 0)
    confirmed = state.confirmed_local_issues()[0]
    assert (confirmed["state"], confirmed["state_reason"]) == (github_state, reason)
    assert d79(tmp_path, state)["state"] == "in progress"


def test_confirmed_view_keeps_facts_recorded_after_a_write_was_queued(tmp_path):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue("closed")], [], {}, NOW)
    clock.at = minutes(1)
    state.queue_github_write(9, "comment", body="note")
    state.update_local_issue(9, merged=True)
    assert state.confirmed_local_issues()[0]["merged"] is True
    assert d79(tmp_path, state)["state"] == "done"


def test_an_older_listing_finishing_late_never_replaces_a_newer_listing(tmp_path):
    state = store(tmp_path)
    state.refresh_local_issues([issue()], [], {}, minutes(5))
    state.refresh_local_issues([issue("closed", "completed")], [], {}, NOW)
    row = state.local_issues()[0]
    assert (row["state"], row["state_reason"], row["observed_at"]) == ("open", None, minutes(5).isoformat())


# Observation provenance (#1025 review 8): reads keep their start time, older listings never
# delete newer observations, and snapshots from before #1025 never read queued intent.

def test_an_older_listing_that_omits_an_issue_keeps_its_newer_observation_and_writes(tmp_path):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue()], [], {}, NOW)
    state.record_issue(issue("closed", "completed"), {}, observed_at=minutes(3))
    clock.at = minutes(1)
    state.queue_github_write(9, "comment", body="note")
    assert state.refresh_local_issues([], [], {}, minutes(2)) == []
    row, = state.local_issues()
    assert (row["state"], row["observed_at"]) == ("closed", minutes(3).isoformat())
    assert len(state.github_outbox()) == 1
    assert d79(tmp_path, state) == {"id": "D79", "issues": [9], "state": "done",
                                    "observed_at": minutes(3).isoformat()}
    # A listing that began later and omits it still removes it, as before.
    assert state.refresh_local_issues([], [], {}, minutes(5)) == [9]
    assert state.local_issues() == [] and state.github_outbox() == []


def test_a_delayed_single_read_is_as_old_as_its_start(tmp_path):
    from types import SimpleNamespace

    from foragent.config import Config
    from foragent.tracker import RecordingTracker

    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue("closed", "completed")], [], {}, NOW)

    class Slow:
        def fetch(self, number):
            clock.at = minutes(2)
            # A listing that began while this read was in flight shows the issue reopened.
            state.refresh_local_issues([issue()], [], {}, minutes(1))
            clock.at = minutes(3)
            return issue("closed", "completed")

    tracker = RecordingTracker(Slow(), SimpleNamespace(store=state, config=Config(), now=lambda: clock.at))
    clock.at = minutes(0.5)
    tracker.fetch(9)
    row, = state.local_issues()
    assert (row["state"], row["observed_at"]) == ("open", minutes(1).isoformat())
    assert d79(tmp_path, state)["state"] == "in progress"


def _legacy_snapshot(state, row, outbox, base):
    """An issues.json written before #1025: no github records."""
    import json

    (state.root / "issues.json").parent.mkdir(parents=True, exist_ok=True)
    (state.root / "issues.json").write_text(json.dumps({
        "refreshed_at": NOW.isoformat(), "issues": {"9": row}, "outbox": outbox,
        "outbox_base": {"9": base}, "outbox_sequence": len(outbox)}))


def test_a_legacy_row_with_an_undelivered_close_reads_its_baseline(tmp_path):
    state = store(tmp_path)
    open_row = {"number": 9, "title": "T", "body": decisions.marker(["D79"]), "labels": [], "lane": "main",
                "state": "open", "pr_number": 3, "merged": True, "comments": []}
    _legacy_snapshot(state, {**open_row, "state": "closed"},
                     [{"id": 1, "issue": 9, "action": "close_issue", "body": None,
                       "queued_at": NOW.isoformat()}], open_row)
    assert state.confirmed_local_issues()[0]["state"] == "open"
    assert d79(tmp_path, state)["state"] == "in progress"


@pytest.mark.parametrize("action, payload, field, observed", [
    ("update_issue", {"title": "T", "body": decisions.marker(["D79"])}, "body", ""),
    ("add_labels", {"labels": ["approved"]}, "labels", []),
])
def test_writes_queued_before_the_first_observation_are_not_observed_facts(tmp_path, action, payload, field,
                                                                            observed):
    state = store(tmp_path)
    state.queue_github_write(9, action, **payload)
    assert state.local_issues()[0][field] != observed, "Local intent shows the write"
    assert state.confirmed_local_issues()[0][field] == observed
    assert d79(tmp_path, state)["issues"] == []


@pytest.mark.parametrize("reason", ["reopened", "not_planned"])
def test_a_changed_close_reason_contradicts_a_queued_close(tmp_path, reason):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue()], [], {}, NOW)
    clock.at = minutes(1)
    state.queue_github_write(9, "close_issue", body=None)
    # Someone closed and reopened it (state matches the baseline, the reason does not),
    # or closed it as not planned: flushing the queued close would be wrong.
    seen = issue("open", "reopened") if reason == "reopened" else issue("closed", "not_planned")
    assert state.refresh_local_issues([seen], [], {}, minutes(2)) == [9]
    assert state.github_outbox() == []
    assert state.local_issues()[0]["state"] == seen.state


def test_a_close_reason_absent_from_an_old_baseline_is_no_contradiction(tmp_path):
    clock = Clock()
    state = clock.store(tmp_path)
    open_row = {"number": 9, "title": "T", "body": decisions.marker(["D79"]), "labels": [], "lane": "main",
                "state": "open", "pr_number": None, "merged": False, "comments": [],
                "observed_at": NOW.isoformat()}
    _legacy_snapshot(state, open_row, [{"id": 1, "issue": 9, "action": "comment", "body": "note",
                                        "queued_at": NOW.isoformat()}], open_row)
    # The old baseline cannot say whether this reopening predates the queued write.
    assert state.refresh_local_issues([issue("open", "reopened")], [], {}, minutes(1)) == []
    assert len(state.github_outbox()) == 1


def test_an_older_single_read_changes_nothing(tmp_path):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue()], [], {}, minutes(5))
    before = (state.root / "issues.json").read_bytes()
    clock.at = minutes(9)
    state.record_issue(issue("closed", "completed"), {}, observed_at=minutes(1))
    assert (state.root / "issues.json").read_bytes() == before


def test_an_older_listing_keeps_a_newer_refresh_failure(tmp_path):
    state = store(tmp_path)
    state.refresh_local_issues([issue()], [], {}, NOW)
    state.record_issues_refresh_failure("network down", minutes(5))
    state.refresh_local_issues([issue()], [], {}, minutes(2))
    assert state.issues_observation()["failed_at"] == minutes(5).isoformat()
    state.refresh_local_issues([issue()], [], {}, minutes(6))
    assert state.issues_observation()["failed_at"] is None


# Every tracker read lands through the one observation writer (#1025 review 9).

def _recording(state, clock, inner):
    from types import SimpleNamespace

    from foragent.config import Config
    from foragent.tracker import RecordingTracker

    return RecordingTracker(inner, SimpleNamespace(store=state, config=Config(), now=lambda: clock.at))


def _events(state):
    import json

    path = state.root / "orch-events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_a_closed_issue_read_records_a_changed_close_reason_of_an_issue_already_closed(tmp_path):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue("closed", "completed")], [], {}, NOW)
    assert d79(tmp_path, state)["state"] == "done"

    class Closed:
        def issues_closed_since(self, since):
            return [issue("closed", "not_planned")]

    clock.at = minutes(5)
    _recording(state, clock, Closed()).observe_closed_issues(observed_at=minutes(5))
    row = state.local_issues()[0]
    assert (row["state_reason"], row["observed_at"]) == ("not_planned", minutes(5).isoformat())
    assert d79(tmp_path, state)["state"] == "in progress"


@pytest.mark.parametrize("method", ["sub_issues", "list_open_issues"])
def test_issues_returned_by_list_reads_are_observations(tmp_path, method):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue("closed", "completed")], [], {}, NOW)

    class Lists:
        def sub_issues(self, number):
            return [issue("closed", "not_planned")]

        def list_open_issues(self, number=None):
            return [issue("closed", "not_planned")]

    clock.at = minutes(2)
    assert [item.number for item in getattr(_recording(state, clock, Lists()), method)(1)] == [9]
    assert state.local_issues()[0]["state_reason"] == "not_planned"
    assert d79(tmp_path, state)["state"] == "in progress"


def test_a_read_that_contradicts_queued_writes_drops_them_and_says_so(tmp_path):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue()], [], {}, NOW)
    clock.at = minutes(1)
    state.queue_github_write(9, "close_issue", body=None)

    class Reader:
        def get_issue(self, number):
            return issue("open", "reopened")

    clock.at = minutes(2)
    _recording(state, clock, Reader()).get_issue(9)
    assert state.github_outbox() == []
    assert [(event["issue"], event["kind"]) for event in _events(state)] == [(9, "outbox-dropped")]


def test_an_older_list_read_changes_nothing(tmp_path):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue()], [], {}, minutes(5))
    before = (state.root / "issues.json").read_bytes()
    state.record_issues([issue("closed", "completed")], {}, observed_at=minutes(1))
    assert (state.root / "issues.json").read_bytes() == before


# Review 10: listings outside refresh, removals and confirmed writes (#1025).

def test_a_listing_outside_refresh_records_a_reopening(tmp_path):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue("closed", "completed")], [], {}, NOW)
    assert d79(tmp_path, state)["state"] == "done"

    class Lister:
        def list_issues(self, state="all"):
            return [issue("open", "reopened")]

    clock.at = minutes(2)
    assert [item.number for item in _recording(state, clock, Lister()).list_issues("open")] == [9]
    assert d79(tmp_path, state)["state"] == "in progress"


@pytest.mark.parametrize("late", ["refresh", "read"])
def test_an_issue_removed_by_a_newer_listing_is_not_recreated_by_an_older_one(tmp_path, late):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue("closed", "completed")], [], {}, NOW)
    state.refresh_local_issues([], [], {}, minutes(5))
    assert state.local_issues() == []
    if late == "refresh":
        state.refresh_local_issues([issue("closed", "completed")], [], {}, minutes(3))
    else:
        state.record_issues([issue("closed", "completed")], {}, observed_at=minutes(3))
    assert state.local_issues() == []
    assert d79(tmp_path, state)["issues"] == []
    state.record_issue(issue(), {}, observed_at=minutes(6))
    assert [row["state"] for row in state.local_issues()] == ["open"], "A newer observation brings it back"


def test_a_confirmed_missing_issue_is_not_recreated_by_an_older_read(tmp_path):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue("closed", "completed")], [], {}, NOW)
    clock.at = minutes(5)
    state.drop_missing_github_issue(9)
    state.record_issue(issue("closed", "completed"), {}, observed_at=minutes(4))
    assert state.local_issues() == []


@pytest.mark.parametrize("seen, action, payload", [
    (issue(labels=["x"]), "add_labels", {"labels": ["x"]}),
    (issue(), "remove_labels", {"labels": ["x"]}),
    (issue(body="same"), "update_issue", {"title": "T", "body": "same"}),
    (issue("closed", "completed"), "close_issue", {"body": None}),
])
def test_a_write_github_already_shows_is_retired_without_a_drop(tmp_path, seen, action, payload):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([seen], [], {}, NOW)
    clock.at = minutes(1)
    state.queue_github_write(9, action, **payload)
    assert state.refresh_local_issues([seen], [], {}, minutes(2)) == [], "A confirmation is not a drop"
    assert state.github_outbox() == []


@pytest.mark.parametrize("seen, writes", [
    (issue("closed", "completed"), [("close_issue", {"body": "Done"})]),
    (issue(), [("add_labels", {"labels": ["x"]}), ("remove_labels", {"labels": ["x"]})]),
    (issue(), [("comment", {"body": "note"})]),
])
def test_writes_github_does_not_yet_show_stay_queued(tmp_path, seen, writes):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([seen], [], {}, NOW)
    clock.at = minutes(1)
    for action, payload in writes:
        state.queue_github_write(9, action, **payload)
    state.refresh_local_issues([seen], [], {}, minutes(2))
    assert [entry["action"] for entry in state.github_outbox()] == [action for action, _ in writes]


def test_a_flushed_write_answer_and_a_missing_check_are_observations(tmp_path):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue("closed", "completed")], [], {}, NOW)
    clock.at = minutes(1)
    state.queue_github_write(9, "add_labels", labels=["x"])

    class Writer:
        def add_labels(self, number, labels):
            return issue("closed", "not_planned", labels=["x"])

        def get_issue(self, number):
            return issue("open", "reopened")

    clock.at = minutes(2)
    tracker = _recording(state, clock, Writer())
    tracker.flush_outbox()
    assert state.local_issues()[0]["state_reason"] == "not_planned"
    clock.at = minutes(3)
    assert tracker._issue_missing(9) is None
    assert state.local_issues()[0]["state_reason"] == "reopened"


def test_a_rejected_read_in_a_batch_leaves_no_placeholder_row(tmp_path):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue("closed", "completed")], [], {}, NOW)
    state.refresh_local_issues([], [], {}, minutes(5))
    other = Issue(10, "Other", "", frozenset(), "open")
    state.record_issues([issue("closed", "completed"), other], {}, observed_at=minutes(3))
    assert [row["number"] for row in state.local_issues()] == [10]


# Review 11: an absence is as old as the lookup that found it (#1025).

def _not_found():
    from foragent.tracker import TrackerError

    return TrackerError("HTTP 404: Not Found (https://api.github.com/repos/o/r/issues/9)")


def test_a_delayed_not_found_lookup_never_removes_a_newer_observation(tmp_path):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue("closed", "completed")], [], {}, NOW)
    clock.at = minutes(1)
    state.queue_github_write(9, "add_labels", labels=["x"])

    class Gone:
        def add_labels(self, number, labels):
            raise _not_found()

        def get_issue(self, number):
            # The lookup begins at T2; a reopening is recorded at T3 before the 404 returns at T4.
            clock.at = minutes(3)
            state.record_issue(issue("open", "reopened"), {}, observed_at=minutes(3))
            clock.at = minutes(4)
            raise _not_found()

    clock.at = minutes(2)
    with pytest.raises(type(_not_found())):
        _recording(state, clock, Gone()).flush_outbox()
    row, = state.local_issues()
    assert (row["state"], row["observed_at"]) == ("open", minutes(3).isoformat())
    assert "9" not in (state._issue_record().get("removed") or {}), "No removal is recorded"
    assert d79(tmp_path, state) == {"id": "D79", "issues": [9], "state": "in progress",
                                    "observed_at": minutes(3).isoformat()}


def test_a_not_found_lookup_removes_the_issue_as_of_its_start(tmp_path):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue("closed", "completed")], [], {}, NOW)
    clock.at = minutes(1)
    state.queue_github_write(9, "add_labels", labels=["x"])

    class Gone:
        def add_labels(self, number, labels):
            raise _not_found()

        def get_issue(self, number):
            clock.at = minutes(4)
            raise _not_found()

    clock.at = minutes(2)
    _recording(state, clock, Gone()).flush_outbox()
    assert state.local_issues() == [] and state.github_outbox() == []
    assert [event["kind"] for event in _events(state)] == ["outbox-dropped"]
    # Stamped with the lookup start (T2): a read that began at T3 is newer and brings it back.
    state.record_issue(issue(), {}, observed_at=minutes(3))
    assert [row["number"] for row in state.local_issues()] == [9]


def test_a_write_queued_after_the_lookup_began_keeps_the_issue(tmp_path):
    clock = Clock()
    state = clock.store(tmp_path)
    state.refresh_local_issues([issue()], [], {}, NOW)
    clock.at = minutes(3)
    state.queue_github_write(9, "comment", body="note")
    assert state.drop_missing_github_issue(9, observed_at=minutes(2)) is False
    assert len(state.github_outbox()) == 1 and len(state.local_issues()) == 1
    assert state.drop_missing_github_issue(9, observed_at=minutes(4)) is True
    assert state.local_issues() == [] and state.github_outbox() == []
