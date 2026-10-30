"""Acceptance test for observation-ingest. Locked by Foragent; do not edit."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from foragent.config import Config
from foragent.state import StateStore
from foragent.tracker import FakeTracker, RecordingTracker, TrackerError
from test_order_progress import offline_local_cli
from test_skills_autosync import cli_exit, isolated_home


NUMBER, PARENT = 7, 99
START = datetime(2026, 10, 31, 9, tzinfo=UTC)
# The first three are the public Tracker read contract, as used by RunContext.
# Also exercise the real CLI's bulk status refresh: it must not bypass that contract.
READS = ("issue-list", "sub-issue", "single-issue", "status-refresh")


@pytest.fixture
def case(tmp_path, monkeypatch, capsys, isolated_home):
    from foragent import cli
    from foragent.launchd import LaunchdStatus

    local = offline_local_cli(tmp_path, monkeypatch)
    clock = [START]
    store = StateStore(local.store.root, now=lambda: clock[0])
    remote = FakeTracker(now=lambda: clock[0])
    remote.add_issue(PARENT, "Order parent")
    remote.add_issue(NUMBER, "Baseline title", "Baseline body", labels=["baseline"])
    remote.add_sub_issue(PARENT, NUMBER)
    # FakeTracker has no deletion API. Model the native edge disappearing with
    # its child so an authoritative missing issue cannot cause a fake KeyError.
    monkeypatch.setattr(remote, "sub_issues", lambda n: (
        [remote.issues[NUMBER]] if n == PARENT and NUMBER in remote.issues else []))
    baseline = remote.get_issue(NUMBER)
    store.refresh_local_issues(remote.list_issues(), [], {}, clock[0])
    context = SimpleNamespace(store=store, config=Config(repo="owner/repo", skill_libraries=[]),
                              now=lambda: clock[0])
    reader = RecordingTracker(remote, context, queue_writes=True)
    monkeypatch.setattr(cli, "GhTracker", lambda repo: remote)
    monkeypatch.setattr(cli, "load_config", lambda root: context.config)
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo, **kw: store))
    monkeypatch.setattr(cli.launchd, "status", lambda *a, **kw: LaunchdStatus("offline", False, False))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0] if tz else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(cli, "datetime", Clock)

    def status(live=False):
        capsys.readouterr()
        args = ["gate", "status", "--json", "--repo-root", str(local.root)]
        code = cli_exit(local.main, args + (["--live"] if live else []))
        output = capsys.readouterr()
        assert code == 0, output.err
        return json.loads(output.out)

    def read(route):
        if route == "issue-list":
            return reader.list_issues()
        if route == "sub-issue":
            return reader.sub_issues(PARENT)
        if route == "single-issue":
            return reader.get_issue(NUMBER)
        return status(live=True)

    def rows():
        # A fresh public reader checks persisted behavior, not an adapter's cache.
        return {row["number"]: row for row in StateStore(store.root).local_issues()}

    return SimpleNamespace(remote=remote, reader=reader, store=store, clock=clock,
                           baseline=baseline, read=read, rows=rows, status=status,
                           gh_calls=local.gh_calls)


def advance(case):
    case.clock[0] += timedelta(seconds=1)


def publish_remote(case, *, state="open", reason=None, title="Newer title", body="Newer body",
                   labels=frozenset({"newer"})):
    issue = replace(case.baseline, title=title, body=body, labels=labels,
                    state=state, state_reason=reason, updated_at=case.clock[0].isoformat())
    case.remote.issues[NUMBER] = issue
    return issue


def assert_facts(case, expected, *, extra_labels=()):
    row = case.rows().get(NUMBER)
    assert row is not None, "A present observation must be persisted"
    actual = (row["title"], row["body"], set(row["labels"]), row["state"], row.get("state_reason"))
    wanted = (expected.title, expected.body, set(expected.labels) | set(extra_labels),
              expected.state, expected.state_reason)
    assert actual == wanted, "The newest observation must replace every issue field together"
    visible = {item["number"]: item for group in case.status()["issues"].values() for item in group}
    assert (NUMBER in visible) == (expected.state == "open")
    if expected.state == "open":
        assert visible[NUMBER]["title"] == expected.title
    assert case.gh_calls == [], "Ordinary gate status must stay offline"


def delay_read(case, monkeypatch, route, during):
    """Delay one fake transport response, without threads or clock sleeps.

    Capture the result before running a newer public operation, then return it
    afterward. Nested calls use the original transport, not this one-shot hook.
    """
    if route == "status-refresh":
        # Isolate the delayed listing. Otherwise the outer refresh would make
        # a genuinely newer child read after its old listing finally returns,
        # and that child's provenance is allowed to advance. A separate test
        # below covers child observations made during bulk refresh.
        monkeypatch.setattr(case.remote, "sub_issues", lambda number: [])
    name = {"issue-list": "list_issues", "sub-issue": "sub_issues",
            "single-issue": "get_issue", "status-refresh": "list_issues"}[route]
    original = getattr(case.remote, name)
    waiting = [True]

    def response(*args, **kwargs):
        result = original(*args, **kwargs)
        if waiting[0]:
            waiting[0] = False
            advance(case)
            during()
            advance(case)
        return result

    monkeypatch.setattr(case.remote, name, response)


@pytest.mark.parametrize("route", READS)
def test_late_older_read_cannot_replace_newer_facts_or_provenance(case, monkeypatch, route):
    """Reject arrival/server-update-time ordering, partial merges and read bypasses."""
    # Server timestamps do not define the ordering of local observations.
    case.remote.issues[NUMBER] = replace(case.baseline, updated_at=(START + timedelta(days=1)).isoformat())
    advance(case)
    saved = {}

    def newer_read():
        saved["issue"] = publish_remote(case, state="closed")
        case.read(route)
        # Verify the newer response was ingested before testing the old response.
        assert_facts(case, saved["issue"])
        saved["row"] = case.rows()[NUMBER]
        saved["outbox"] = case.store.github_outbox()

    delay_read(case, monkeypatch, route, newer_read)
    case.read(route)
    assert case.rows()[NUMBER] == saved["row"], "An older read must change no facts or provenance"
    assert case.store.github_outbox() == saved["outbox"]
    assert_facts(case, saved["issue"])

    # Repeat with a local overlay created after the newer observation. Ignoring
    # stale visible fields alone must not discard that overlay or its baseline.
    advance(case)

    def newer_then_overlay():
        saved["issue"] = publish_remote(case, title="Newest title", body="Newest body")
        case.read(route)
        assert_facts(case, saved["issue"])
        advance(case)
        case.store.queue_github_write(NUMBER, "add_labels", labels=["pending"])
        saved["row"] = case.rows()[NUMBER]
        saved["outbox"] = case.store.github_outbox()

    delay_read(case, monkeypatch, route, newer_then_overlay)
    case.read(route)
    assert case.rows()[NUMBER] == saved["row"], "A stale read must leave the newer overlay intact"
    assert case.store.github_outbox() == saved["outbox"]
    advance(case)
    confirmed = replace(saved["issue"], labels=saved["issue"].labels | {"pending"})
    case.remote.issues[NUMBER] = confirmed
    case.read(route)
    assert not case.store.github_outbox(), "The stale read must not damage later overlay confirmation"
    assert_facts(case, confirmed)


@pytest.mark.parametrize("route", READS)
@pytest.mark.parametrize("action", ("update_issue", "add_labels", "close_issue"))
def test_delayed_write_ack_cannot_restore_intent_contradicted_by_newer_read(
    case, monkeypatch, route, action,
):
    """Reject acknowledgment-time writes, including body, label and state writes."""
    advance(case)
    original = getattr(case.remote, action)
    expected = {}

    def write_then_wait(*args, **kwargs):
        result = original(*args, **kwargs)
        advance(case)
        expected["issue"] = publish_remote(case)  # Contradicts all three old intents.
        case.read(route)
        advance(case)
        return result

    monkeypatch.setattr(case.remote, action, write_then_wait)
    if action == "update_issue":
        case.reader.update_issue(NUMBER, title="Local title", body="Local body")
    elif action == "add_labels":
        case.reader.add_labels(NUMBER, ["local-intent"])
    else:
        case.reader.close_issue(NUMBER)
    assert_facts(case, expected["issue"])
    assert not case.store.github_outbox(), "A retired intent must not be queued again by its acknowledgment"
    advance(case)
    case.read(route)
    assert_facts(case, expected["issue"])


@pytest.mark.parametrize("route", READS)
def test_reopen_after_completed_clears_old_closure_evidence(case, route):
    """Reject monotone completion and retaining a closed issue's completed reason."""
    closed = publish_remote(case, state="closed", reason="completed")
    # Seed an existing persisted completed fact through the public store API,
    # as the existing decision-tracking acceptance fixture does. The read under
    # test, not fixture setup, must clear it on reopening.
    case.store.refresh_local_issues([closed, case.remote.get_issue(PARENT)], [], {}, case.clock[0])
    case.store.update_local_issue(NUMBER, state_reason="completed")
    advance(case)
    opened = publish_remote(case)
    case.read(route)
    assert_facts(case, opened)
    advance(case)
    case.read(route)
    assert_facts(case, opened)


@pytest.mark.parametrize("route", READS)
def test_completed_to_not_planned_on_already_closed_issue(case, route):
    """Reject skipping closed-to-closed observations or updating only open/state."""
    closed = publish_remote(case, state="closed", reason="completed")
    case.store.refresh_local_issues([closed, case.remote.get_issue(PARENT)], [], {}, case.clock[0])
    case.store.update_local_issue(NUMBER, state_reason="completed")
    advance(case)
    cancelled = publish_remote(case, state="closed", reason="not_planned")
    case.read(route)
    assert_facts(case, cancelled)


@pytest.mark.parametrize("route", READS)
def test_delayed_missing_response_preserves_newer_read_and_pending_write(case, monkeypatch, route):
    """Reject unconditional 404 deletion AND ignoring all missing observations.

    Absence comes from a single-issue 404; the protecting newer observation
    comes through each route. A sub-issue omission is not proof of deletion.
    """
    original = case.remote.get_issue
    expected = {}
    waiting = [True]

    def missing(number):
        if number != NUMBER:
            return original(number)
        if waiting[0]:
            waiting[0] = False
            # Let the nested read succeed while the old missing lookup waits.
            monkeypatch.setattr(case.remote, "get_issue", original)
            advance(case)
            expected["issue"] = publish_remote(case)
            case.read(route)
            advance(case)
            case.store.queue_github_write(NUMBER, "add_labels", labels=["pending"])
            expected["outbox"] = case.store.github_outbox()
            advance(case)
        raise TrackerError("gh: issue not found (HTTP 404)")

    advance(case)
    monkeypatch.setattr(case.remote, "get_issue", missing)
    with pytest.raises(TrackerError, match="HTTP 404"):
        case.reader.get_issue(NUMBER)
    assert_facts(case, expected["issue"], extra_labels=["pending"])
    assert case.store.github_outbox() == expected["outbox"], "Stale absence must preserve pending writes"
    # A genuinely newer 404 must still remove the issue and its pending writes.
    advance(case)
    monkeypatch.setattr(case.remote, "get_issue", missing)
    with pytest.raises(TrackerError, match="HTTP 404"):
        case.reader.get_issue(NUMBER)
    assert NUMBER not in case.rows(), "A newer confirmed missing issue must disappear locally"
    assert not case.store.github_outbox(), "Only the newer absence retires this issue's writes"


@pytest.mark.parametrize("route", READS)
@pytest.mark.parametrize("action,payload", (
    ("add_labels", {"labels": ["baseline"]}),
    ("remove_labels", {"labels": ["already-absent"]}),
    ("update_issue", {"title": "Baseline title", "body": "Baseline body"}),
    ("close_issue", {}),
))
def test_confirmation_matching_prior_baseline_retires_pending_write(case, route, action, payload):
    """Reject baseline-difference-only retirement and pending-read short circuits."""
    if action == "close_issue":
        closed = publish_remote(case, state="closed", reason="completed", labels=frozenset({"baseline"}))
        case.store.refresh_local_issues([closed, case.remote.get_issue(PARENT)], [], {}, case.clock[0])
        case.store.update_local_issue(NUMBER, state_reason="completed")
    advance(case)
    case.store.queue_github_write(NUMBER, action, **payload)
    # Equal read-start/write-start times qualify too, not just strictly later reads.
    # Same write effect as the baseline, but unrelated fields really changed.
    expected = publish_remote(case, title=payload.get("title", "Changed while pending"),
                              body=payload.get("body", "Changed while pending"),
                              labels=frozenset({"baseline"}),
                              state="closed" if action == "close_issue" else "open",
                              reason="completed" if action == "close_issue" else None)
    case.read(route)
    assert not case.store.github_outbox(), "A newer confirming observation must retire even a no-op write"
    assert_facts(case, expected)


def test_complete_absence_rejects_stale_resurrection_but_filtered_omission_is_not_absence(
    case, monkeypatch,
):
    """Reject deletion without tombstones, and treating filtered listings as complete."""
    advance(case)

    def newer_absence():
        case.remote.issues.pop(NUMBER)
        # Keep PARENT: this is a valid nonempty complete issue listing.
        case.read("status-refresh")
        assert NUMBER not in case.rows(), "A complete newer listing establishes absence"

    delay_read(case, monkeypatch, "status-refresh", newer_absence)
    case.read("status-refresh")
    assert NUMBER not in case.rows(), "An older listing must not resurrect an issue after newer absence"
    advance(case)
    present = publish_remote(case, state="closed", reason="completed")
    case.read("status-refresh")
    assert_facts(case, present)
    advance(case)
    case.reader.list_issues("open")
    assert_facts(case, present)  # Closed issue omitted by an open-only listing still exists.


def test_status_refresh_ingests_sub_issue_facts_read_after_the_listing(case, monkeypatch):
    """Reject fixing only public wrappers while bulk refresh discards child facts."""
    expected = {}
    children = case.remote.sub_issues

    def after_listing(number):
        if number == PARENT:
            advance(case)
            expected["issue"] = publish_remote(case)
        return children(number)

    monkeypatch.setattr(case.remote, "sub_issues", after_listing)
    advance(case)
    case.status(live=True)
    assert_facts(case, expected["issue"])


def test_light_closed_issue_refresh_replaces_reason_on_an_already_closed_issue(case):
    """Reject a closed-issue refresh that only records open-to-closed transitions."""
    closed = publish_remote(case, state="closed", reason="completed")
    case.store.refresh_local_issues([closed, case.remote.get_issue(PARENT)], [], {}, case.clock[0])
    case.store.update_local_issue(NUMBER, state_reason="completed")
    case.store.record_closed_issue_watermark(case.clock[0])
    advance(case)
    cancelled = publish_remote(case, state="closed", reason="not_planned")
    case.reader.observe_closed_issues(observed_at=case.clock[0])
    assert_facts(case, cancelled)
