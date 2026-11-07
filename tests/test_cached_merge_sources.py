"""GitHub merges belong to their opening execution; local merge clocks have one journal."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from foragent import presence, star
from foragent.config import Config
from foragent.state import StateStore
from foragent.tracker import FakeTracker, Issue, PullRequest, RecordingTracker
from foragent.watch_sections import progress


NOW = datetime(2026, 11, 4, tzinfo=UTC)


def executions(tmp_path):
    clock = [NOW]
    store = StateStore(tmp_path, now=lambda: clock[0])
    old = store.start_run(7, branch="old", worktree="old")
    clock[0] += timedelta(minutes=2)
    store.finish_run(7, "error")
    clock[0] += timedelta(minutes=1)
    new = store.start_run(7, branch="new", worktree="new")
    return store, clock, old, new


def observe(store, pr, at):
    issue = Issue(7, "Feature", "", frozenset({"approved"}), "closed", updated_at=at.isoformat())
    store.refresh_local_issues([issue], [pr], {}, at, complete_prs=True)
    return store.local_issues()[0]["merges"][str(pr.number)]


def test_pr_creation_links_the_opening_run_and_refresh_never_reassigns_it(tmp_path):
    store, clock, old, new = executions(tmp_path)
    merged = NOW + timedelta(minutes=5)
    pr = PullRequest(70, "foragent/issue-7", "main", "", "", merged.isoformat(),
                     "https://github.com/o/r/pull/70", (NOW + timedelta(minutes=1)).isoformat())
    entry = observe(store, pr, merged)
    assert entry["started_at"] == old.started_at
    assert entry["url"] == pr.url and entry["created_at"] == pr.created_at
    assert store.run_merged(old) and not store.run_merged(new)
    assert store.merge_times() == {(7, old.started_at): pr.merged_at}
    assert progress.merged_work(store)[0][2] == NOW
    clock[0] = merged + timedelta(minutes=1)
    store.finish_run(7, "error")
    later = store.start_run(7, branch="later", worktree="later")
    # A later listing, even with different creation metadata, cannot transfer ownership.
    assert observe(store, replace(pr, created_at=later.started_at), clock[0]) == entry
    assert not store.run_merged(later)
    assert store.merge_times() == {(7, old.started_at): pr.merged_at}


@pytest.mark.parametrize("refresh_first", [False, True])
def test_creation_time_names_the_owner_even_when_a_retry_journals_the_merge(tmp_path, refresh_first):
    store, clock, old, new = executions(tmp_path)
    merged = NOW + timedelta(minutes=5)
    pr = PullRequest(70, "foragent/issue-7", "main", "", "", merged.isoformat(),
                     created_at=(NOW + timedelta(minutes=1)).isoformat())
    if refresh_first:
        observe(store, pr, merged)
    else:
        store.record_merge(7, merged, pr=70)
        assert store.run_merged(new), "Until a refresh caches the PR, the gate's journal row counts"
        assert observe(store, pr, merged)["started_at"] == old.started_at
    store.record_merge(7, merged, pr=70)  # Journaling after the refresh adds no second owner.
    assert store.local_issues()[0]["merges"]["70"]["started_at"] == old.started_at
    assert store.run_merged(old) and not store.run_merged(new)
    assert set(store.merge_times()) == {(7, old.started_at)}
    store.finish_run(7, "merged")
    retry, = [run for run in store.read_history() if run.started_at == new.started_at]
    assert not store.run_merged(retry), "A retry that merged an earlier run's PR is not merged"
    assert progress.merged_work(store) == [(7, "m", NOW, merged)]


@pytest.mark.parametrize("created", [None, NOW - timedelta(days=1)])
def test_missing_opening_run_infers_no_owner(tmp_path, created):
    store, clock, old, new = executions(tmp_path)
    merged = NOW + timedelta(minutes=5)
    pr = PullRequest(70, "foragent/issue-7", "main", "", "", merged.isoformat(),
                     created_at=created.isoformat() if created else None)
    entry = observe(store, pr, merged)
    assert entry["started_at"] is None and "created_at" not in entry
    assert not store.run_merged(new) and not store.run_merged(old)
    store.record_merge(7, merged, pr=70)
    assert store.run_merged(new) and not store.run_merged(old), (
        "An unknown opener removes no recorded merge, and journal order assigns no new owner")


def test_refresh_does_not_reinterpret_a_legacy_cache_link(tmp_path):
    store, clock, old, new = executions(tmp_path)
    merged = NOW + timedelta(minutes=5)
    legacy = {"merged_at": merged.isoformat(), "approved": True, "closed": True, "size": "m",
              "started_at": new.started_at}
    issue = Issue(7, "Feature", "", frozenset({"approved"}), "closed", updated_at=merged.isoformat())
    store.refresh_local_issues([issue], [], {}, merged)
    store.update_local_issue(7, merges={"70": legacy})
    pr = PullRequest(70, "foragent/issue-7", "main", "", "", merged.isoformat(),
                     created_at=(NOW + timedelta(minutes=1)).isoformat())
    entry = observe(store, pr, merged + timedelta(minutes=1))
    assert (entry["started_at"], "created_at" in entry) == (new.started_at, False)
    assert not store.run_merged(new) and not store.run_merged(old), "A legacy link is no ownership"
    store.finish_run(7, "merged")
    retry, = [run for run in store.read_history() if run.started_at == new.started_at]
    assert store.run_merged(retry), "A legacy merged outcome stays merged without a cached creation time"


@pytest.mark.parametrize("opener", ["old", "new"])
def test_legacy_merged_outcome_is_disowned_only_by_a_cached_creation_time_of_another_run(tmp_path, opener):
    store, clock, old, new = executions(tmp_path)
    merged = NOW + timedelta(minutes=5)
    clock[0] = merged
    store.finish_run(7, "merged")  # A legacy gate merge: no journal row names its PR.
    created = NOW + timedelta(minutes=1) if opener == "old" else NOW + timedelta(minutes=4)
    pr = PullRequest(70, "foragent/issue-7", "main", "", "", merged.isoformat(), created_at=created.isoformat())
    observe(store, pr, merged)
    retry, = [run for run in store.read_history() if run.started_at == new.started_at]
    first, = [run for run in store.read_history() if run.started_at == old.started_at]
    assert store.run_merged(retry) is (opener == "new")
    assert store.run_merged(first) is (opener == "old")
    from foragent import tidy
    assert tidy._merged_runs(store) == 1, "tidy counts the one owner"


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("skip", ["stale", "local"])
def test_a_pass_that_skipped_an_issues_pr_facts_keeps_the_merged_pr_watermark(tmp_path, skip, existing):
    store = StateStore(tmp_path, now=lambda: NOW)
    first, middle, last = NOW + timedelta(hours=1), NOW + timedelta(hours=2), NOW + timedelta(hours=3)
    issue = Issue(7, "Feature", "", frozenset({"approved"}), "closed", updated_at=NOW.isoformat())
    pr = PullRequest(70, "foragent/issue-7", "main", "", "", NOW.isoformat(), created_at=NOW.isoformat())
    store.refresh_local_issues([issue], [pr] if existing else [], {}, first, complete_prs=True,
                               merged_prs_watermark=first)
    revision = None
    if skip == "stale":  # A newer observation of #7 (without a watermark of its own).
        store.refresh_local_issues([issue], [pr] if existing else [], {}, last, complete_prs=True)
    else:  # A concurrent local write to #7 during the pass.
        revision = store.issues_revision()
        store.update_local_issue(7, comments=[{"id": 1, "body": "concurrent"}])
        assert store.issues_revision() > revision, "Fixture: the concurrent write landed"
    store.refresh_local_issues([issue], [pr], {}, middle, complete_prs=True, since_revision=revision,
                               merged_prs_watermark=middle)
    assert store._issue_record()["merged_prs_watermark"] == first.isoformat(), (
        "The watermark must not pass an issue whose PR facts this pass skipped")
    later = last + timedelta(hours=1)
    store.refresh_local_issues([issue], [pr], {}, later, complete_prs=True, merged_prs_watermark=later)
    assert list(store.local_issues()[0]["merges"]) == ["70"]
    assert store._issue_record()["merged_prs_watermark"] == later.isoformat(), "Once recorded, it advances"


def test_gate_journal_is_visible_before_finish_and_records_pr_identity(tmp_path):
    store = StateStore(tmp_path, now=lambda: NOW)
    run = store.start_run(7, branch="branch", worktree="worktree")
    store.update_run(7, last_review_pass_at=NOW.isoformat())
    merged = NOW + timedelta(seconds=30)
    store.record_merge(7, merged, pr=70)
    reopened = StateStore(tmp_path)
    assert reopened.merge_times() == {(7, run.started_at): merged.isoformat()}
    assert reopened.run_merged(reopened.get_run(7))
    assert presence.gate_merges(reopened) == reopened.merge_times()
    entry, = [json.loads(line) for line in (tmp_path / "merges.jsonl").read_text().splitlines()]
    assert entry == {"kind": "gate", "number": 7, "started_at": run.started_at,
                     "merged_at": merged.isoformat(), "pr": 70}
    store.finish_run(7, "merged")
    for path in (tmp_path / "runs/7.json", tmp_path / "runs.jsonl"):
        for line in ([path.read_text()] if path.suffix == ".json" else path.read_text().splitlines()):
            value = json.loads(line)
            assert not {"merged_at", "owner_merge", "owner_merge_started_at"} & value.keys()
    assert store.get_run(7).review_pass_to_merge_seconds == 30


def test_owner_append_response_loss_retries_without_a_duplicate_or_run_copy(tmp_path, monkeypatch):
    store = StateStore(tmp_path, now=lambda: NOW)
    run = store.start_run(7, branch="branch", worktree="worktree")
    store.finish_run(7, "merge-queued")
    history = (tmp_path / "runs.jsonl").read_bytes()
    append = store._append_merge
    fact = {"pr": 70, "merged_at": (NOW + timedelta(minutes=1)).isoformat()}

    def lost_response(entry):
        append(entry)
        raise OSError("response lost")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_append_merge", lost_response)
        with pytest.raises(OSError, match="response lost"):
            store.record_owner_merge(7, run.started_at, fact)
    reopened = StateStore(tmp_path)
    assert reopened.owner_merge_facts() == {(7, run.started_at): fact}
    reopened.record_owner_merge(7, run.started_at, fact)
    assert len((tmp_path / "merges.jsonl").read_text().splitlines()) == 1
    assert (tmp_path / "runs.jsonl").read_bytes() == history


@pytest.mark.parametrize("merged_at", [
    "unknown", "2026-11-04T00:00:01", "2026-11-03T23:59:59+00:00", None,
])
def test_invalid_owner_times_are_rejected_by_writer_and_reader(tmp_path, merged_at):
    store = StateStore(tmp_path, now=lambda: NOW)
    run = store.start_run(7, branch="branch", worktree="worktree")
    fact = {"pr": 70, "merged_at": merged_at}
    with pytest.raises(ValueError, match="Merge time"):
        store.record_owner_merge(7, run.started_at, fact)
    assert not (tmp_path / "merges.jsonl").exists()
    with store._state_lock():
        store._append_merge({"kind": "owner", "number": 7, "started_at": run.started_at,
                             "merged_at": merged_at, "pr": 70, "fact": fact})
    assert store.owner_merge_facts() == {} and store.merge_times() == {}
    assert not store.run_merged(run)


def test_star_counts_journals_and_only_cache_links_to_runs_in_the_same_folder(tmp_path):
    first = StateStore(tmp_path / "one", now=lambda: NOW)
    run = first.start_run(7, branch="branch", worktree="worktree")
    merged = NOW + timedelta(seconds=1)
    pr = PullRequest(70, "foragent/issue-7", "main", "", "", merged.isoformat(), created_at=merged.isoformat())
    observe(first, pr, merged)
    # This merge belongs to a run absent from this repository folder.
    first.update_local_issue(8, merges={"80": {"started_at": run.started_at, "merged_at": merged.isoformat(),
                                               "created_at": merged.isoformat()}})
    second = StateStore(tmp_path / "two", now=lambda: NOW)
    second.start_run(7, branch="branch", worktree="worktree")
    second.record_merge(7, merged, pr=71)
    (second.root / "runs/7.json").unlink()  # A durable journal still counts after snapshot pruning.
    assert star.local_merges(tmp_path) == 2
    first.record_merge(7, merged, pr=70)
    assert star.local_merges(tmp_path) == 2, "A merge seen through both sources counts once per repo issue"


def test_failed_refresh_keeps_watermark_and_legacy_file_until_retry(tmp_path, monkeypatch):
    class Incremental(FakeTracker):
        def merged_prs_updated_since(self, since, head_prefix=""):
            self.since.append(since)
            return []

    tracker = Incremental(now=lambda: NOW)
    tracker.since = []
    tracker.add_issue(7, "Feature", "")
    store = StateStore(tmp_path, now=lambda: NOW)
    legacy = tmp_path / "merged_prs.json"
    legacy.write_text(json.dumps({"watermark": NOW.isoformat(), "prs": []}))
    context = SimpleNamespace(store=store, now=lambda: NOW, config=Config())
    recording = RecordingTracker(tracker, context)
    write = store._write_json

    def fail_snapshot(path, value):
        if path == tmp_path / "issues.json":
            raise OSError("snapshot failed")
        write(path, value)

    with monkeypatch.context() as patch:
        patch.setattr(store, "_write_json", fail_snapshot)
        with pytest.raises(OSError, match="snapshot failed"):
            recording.refresh_issues()
    assert store._issue_record().get("merged_prs_watermark") is None and legacy.exists()
    recording.refresh_issues()
    assert tracker.since == [None, None]
    assert store._issue_record()["merged_prs_watermark"] == NOW.isoformat()
    assert not legacy.exists()


@pytest.mark.parametrize("reverse", [False, True])
def test_cache_merge_time_is_the_latest_for_its_run_in_either_listing_order(tmp_path, reverse):
    store = StateStore(tmp_path, now=lambda: NOW)
    run = store.start_run(7, branch="branch", worktree="worktree")
    prs = [PullRequest(70 + minute, "foragent/issue-7", "main", "", "",
                       (NOW + timedelta(minutes=minute)).isoformat(), created_at=NOW.isoformat())
           for minute in (1, 2)]
    issue = Issue(7, "Feature", "", frozenset({"approved"}), "closed")
    store.refresh_local_issues([issue], list(reversed(prs)) if reverse else prs, {}, NOW)
    assert store.merge_times() == {(7, run.started_at): prs[-1].merged_at}


@pytest.mark.parametrize("read", ["get_pr", "list_prs", "merged_prs_updated_since"])
def test_github_pr_reads_preserve_creation_time(tmp_path, read):
    from foragent.tracker import GhTracker

    created = (NOW - timedelta(minutes=1)).isoformat()
    item = {"number": 70, "title": "Feature", "body": "", "state": "closed",
            "head": {"ref": "foragent/issue-7", "sha": "head"}, "base": {"ref": "main"},
            "html_url": "https://github.com/o/r/pull/70", "merged_at": NOW.isoformat(),
            "created_at": created, "updated_at": NOW.isoformat()}
    calls = []

    def response(argv, input=None):
        calls.append(argv)
        return json.dumps(item if read == "get_pr" else [item])

    tracker = GhTracker("o/r", run=response)
    if read == "get_pr":
        assert tracker.get_pr(70).created_at == created
    elif read == "list_prs":
        pr, = tracker.list_prs("merged", "foragent/")
        assert pr.created_at == created
    else:
        pr, = tracker.merged_prs_updated_since(None, "foragent/")
        assert pr["created_at"] == created
    assert len(calls) == 1


@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.parametrize("closed", [False, True])
def test_cached_merge_samples_keep_frozen_approval_and_closure_for_finished_runs(tmp_path, approved, closed):
    store = StateStore(tmp_path, now=lambda: NOW)
    run = store.start_run(7, branch="branch", worktree="worktree")
    store.finish_run(7, "error")
    merged = NOW + timedelta(minutes=1)
    pr = PullRequest(70, "foragent/issue-7", "main", "", "", merged.isoformat(), created_at=NOW.isoformat())
    issue = Issue(7, "Feature", "", frozenset({"approved"}) if approved else frozenset(),
                  "closed" if closed else "open", updated_at=merged.isoformat())
    store.refresh_local_issues([issue], [pr], {}, merged)
    assert store.run_merged(run), "A GitHub merge remains a fact regardless of its sampling eligibility"
    assert progress.merged_work(store) == ([(7, "m", NOW, merged)] if approved and closed else [])


@pytest.mark.parametrize("fact", [None, {}, {"merged_at": NOW.isoformat()}])
def test_invalid_owner_journal_cannot_claim_a_prs_run(tmp_path, fact):
    store, clock, old, new = executions(tmp_path)
    merged = NOW + timedelta(minutes=5)
    with store._state_lock():
        store._append_merge({"kind": "owner", "number": 7, "started_at": old.started_at,
                             "merged_at": merged.isoformat(), "pr": 70, "fact": fact})
    pr = PullRequest(70, "foragent/issue-7", "main", "", "", merged.isoformat(),
                     created_at=(NOW + timedelta(minutes=4)).isoformat())
    assert observe(store, pr, merged)["started_at"] == new.started_at
    assert store.run_merged(new) and not store.run_merged(old)


def test_closed_run_guard_ignores_legacy_run_merge_times_and_reads_the_journal(tmp_path):
    from foragent import scheduler  # noqa: F401 (load its modules in their facade order)
    from foragent import scheduler_stop

    store = StateStore(tmp_path, now=lambda: NOW)
    run = store.start_run(7, branch="branch", worktree="worktree")
    path = tmp_path / "runs/7.json"
    value = json.loads(path.read_text())
    value["merged_at"] = NOW.isoformat()
    path.write_text(json.dumps(value))
    ctx = SimpleNamespace(store=store)
    assert not scheduler_stop._merged_own_issue(ctx, run)
    store.record_merge(7, NOW, pr=70)
    assert scheduler_stop._merged_own_issue(ctx, run)


def test_scorecard_skills_and_close_count_a_retrys_merge_of_an_earlier_pr_once(tmp_path):
    from foragent import orch_close, orch_scorecard_records, skills

    store, clock, old, new = executions(tmp_path)
    merged = NOW + timedelta(minutes=5)
    store.record_merge(7, merged, pr=70)
    clock[0] = merged
    store.finish_run(7, "merged")  # Retry B merged A's PR #70 and recorded "merged".
    pr = PullRequest(70, "foragent/issue-7", "main", "", "", merged.isoformat(),
                     created_at=(NOW + timedelta(minutes=1)).isoformat())
    observe(store, pr, merged)
    rows = orch_scorecard_records._executions(store, merged)
    assert [(row["started"].isoformat(), row["merged"]) for row in rows] == [
        (old.started_at, True), (new.started_at, False)], "The scorecard counts the opener's merge once"
    assert [row["merged"] for row in skills._executions(store)] == [True, False]
    ctx = SimpleNamespace(store=store, tracker=SimpleNamespace(list_prs=lambda *args: []))
    assert orch_close._merged_issues(ctx) == {7}
