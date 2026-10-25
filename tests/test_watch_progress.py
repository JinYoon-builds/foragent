from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import os
from types import SimpleNamespace

from foragent.config import Config
from foragent.watch_sections import progress

NOW = datetime(2026, 10, 20, 8, tzinfo=UTC)


def snapshot(**values):
    base = dict(total=0, completed=0, orders=0, eta=None, recent=0, quota={}, captured_at=NOW,
                eta_range=None, eta_basis={}, today=0, status={"active_runs": []})
    return SimpleNamespace(**{**base, **values})


def plain(lines):
    return ["".join(value for value, style in line) for line in lines]


def test_totals_sum_every_open_order_and_skip_closed_orders():
    rows = [
        {"number": 1, "state": "open"}, {"number": 2, "state": "open"}, {"number": 3, "state": "closed"},
        {"number": 10, "state": "closed", "parent": 1}, {"number": 11, "state": "open", "parent": 1},
        {"number": 20, "state": "open", "parent": 2},
        {"number": 30, "state": "closed", "parent": 3}, {"number": 40, "state": "open"},
    ]
    assert progress.totals(rows) == (1, 3, 2)


def test_size_label_alone_decides_the_size_and_otherwise_counts_as_m():
    assert progress.size_of(["size:l", "lane:api"]) == "l"
    assert progress.size_of(["lane:api"]) == "m"
    assert progress.size_of(["size:s", "size:l"]) == "m"


def test_expected_uses_the_default_band_until_three_runs_then_median_and_quartiles():
    assert progress.expected([60.0, 120.0], "s") == (900.0, 1800.0, 3600.0, None)
    assert progress.expected([1.0, 2.0, 3.0], "l") == (1.5, 2.0, 2.5, 3)


def test_estimate_sums_remaining_sizes_over_the_budget_and_none_without_work():
    samples = {"l": [3600.0 * hours for hours in (4, 8, 24)]}
    finish, low, high, basis = progress.estimate(["l", "m"], samples, 2, NOW)
    assert finish == NOW + timedelta(minutes=(8 * 60 + 90) / 2)
    assert low < finish < high and basis == {"m": None, "l": 3}
    assert progress.estimate([], samples, 2, NOW) is None


def test_parallel_budget_prefers_the_machine_budget_then_the_ceiling(tmp_path, monkeypatch):
    from foragent import capacity
    from foragent.state import StateStore

    store = StateStore(tmp_path / "machine-data" / "repo")
    assert progress.parallel_budget(store, Config()) == 1
    assert progress.parallel_budget(store, replace(Config(), max_parallel=3)) == 3
    capacity.write_state(store, 5, NOW)
    assert progress.parallel_budget(store, replace(Config(), max_parallel=3)) == 5


def test_render_words_in_both_languages():
    eta = NOW + timedelta(minutes=30)
    for language, finish, basis, evidence in (
            ("ko", "쯤 끝나요", "size:m 작업 4개", "최근 3시간에 2개 끝냈어요"),
            ("en", "done around", "size:m 4 runs", "2 finished in the last 3 hours")):
        lines = plain(progress.render(snapshot(total=10, completed=4, orders=2, eta=eta, recent=2,
                                               eta_range=(eta, eta), eta_basis={"m": 4}),
                                      replace(Config(), language=language), None, now=NOW))
        assert "4/10" in lines[0] and "▓" in lines[0]
        assert finish in lines[1] and basis in lines[2] and evidence in lines[3]
    quiet = plain(progress.render(snapshot(total=10, completed=4, orders=1, today=3, status={"active_runs": [
        {"number": 7, "phase": "reviewing"}]}), Config(), None, now=NOW))
    assert quiet[1:] == ["nothing finished in the last 3 hours · 6 left · 3 finished today", "running #7 reviewing"]
    done = plain(progress.render(snapshot(total=3, completed=3, orders=1), Config(), None, now=NOW))
    assert done[1:] == ["all done"]
    assert progress.render(snapshot(), Config(), None, now=NOW) == []


def test_finish_time_on_another_day_names_the_day():
    tomorrow = NOW.astimezone() + timedelta(days=1)
    lines = plain(progress.render(snapshot(total=2, completed=1, orders=1, eta=tomorrow, recent=1,
                                           eta_basis={"m": None}),
                                  replace(Config(), language="ko"), None, now=NOW))
    assert lines[1].startswith("내일 ")
    assert "size:m 기본값" in lines[2]


def test_quota_reads_latest_executor_events_and_drops_past_resets(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    future, past = int((NOW + timedelta(hours=3)).timestamp()), int((NOW - timedelta(hours=1)).timestamp())
    claude = {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
        "five_hour": {"utilization": 0.06, "resetsAt": future},
        "seven_day": {"utilization": 0.4, "resetsAt": future + 3600}}}}
    stamp = lambda minutes: (NOW - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
    codex = lambda percent, reset, minutes: {"timestamp": stamp(minutes), "type": "event_msg", "payload": {
        "type": "token_count", "rate_limits": {
            "primary": {"used_percent": percent, "resets_at": reset}, "secondary": None}}}
    (logs / "issue-1.log").write_text("\n".join([
        json.dumps(codex(10, future, 30)), "{broken rate_limit", json.dumps(claude), json.dumps(codex(67, future, 20)),
    ]) + "\n")
    (logs / "issue-2.log").write_text(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "unifiedWindows": {"five_hour": {"utilization": 0.9, "resetsAt": past}}}}) + "\n")
    # issue-3 holds the newest codex reading by its own time; that window already reset.
    (logs / "issue-3.log").write_text(json.dumps(codex(99, past, 5)) + "\n")
    os.utime(logs / "issue-3.log", (1, 1))  # file times never decide which reading is newer
    usage = progress.quota(logs, NOW)
    # Claude records no time: the window that resets later is the newer one.
    assert set(usage) == {"claude"} and round(usage["claude"][0]) == 40
    (logs / "issue-3.log").write_text(json.dumps(codex(99, past, 40)) + "\n")
    usage = progress.quota(logs, NOW)
    assert set(usage) == {"codex", "claude"}
    assert round(usage["codex"][0]) == 67 and round(usage["claude"][0]) == 40
    assert progress.quota(tmp_path / "missing", NOW) == {}
    line = plain(progress.render(snapshot(quota=usage), replace(Config(), language="ko"), None, now=NOW))[0]
    reset = datetime.fromtimestamp(future, UTC).astimezone().strftime("%H:%M")
    assert line.startswith("쿼터 ") and "Codex 67% 썼어요" in line and f"{reset} 리셋" in line


def test_expired_primary_window_does_not_hide_a_valid_secondary_window(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    future, past = int((NOW + timedelta(days=2)).timestamp()), int((NOW - timedelta(minutes=1)).timestamp())
    (logs / "issue-1.log").write_text(json.dumps({"type": "event_msg", "payload": {
        "type": "token_count", "rate_limits": {"primary": {"used_percent": 90, "resets_at": past},
                                               "secondary": {"used_percent": 40, "resets_at": future}}}}) + "\n")
    (logs / "issue-2.log").write_text(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "unifiedWindows": {"five_hour": {"utilization": 0.9, "resetsAt": past},
                           "seven_day": {"utilization": 0.4, "resetsAt": future}}}}) + "\n")
    usage = progress.quota(logs, NOW)
    reset = datetime.fromtimestamp(future, UTC)
    assert usage == {"codex": (40.0, reset), "claude": (40.0, reset)}


def codex_event(percent, reset):
    return json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
        "primary": {"used_percent": percent, "resets_at": reset}, "secondary": None}}})


def test_quota_survives_many_newer_logs_and_large_output(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    future = int((NOW + timedelta(hours=3)).timestamp())
    # The only codex reading is at the start of an old, large log.
    (logs / "issue-1.log").write_text(codex_event(55, future) + "\n" + ("x" * 1023 + "\n") * 600)
    os.utime(logs / "issue-1.log", (1, 1))
    for number in range(2, 14):
        (logs / f"issue-{number}.log").write_text(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "unifiedWindows": {"five_hour": {"utilization": number / 100, "resetsAt": future}}}}) + "\n")
        os.utime(logs / f"issue-{number}.log", (number, number))
    usage = progress.quota(logs, NOW)
    assert round(usage["codex"][0]) == 55 and round(usage["claude"][0]) == 13


def test_quota_reads_appended_lines_and_rewritten_logs(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    future = int((NOW + timedelta(hours=3)).timestamp())
    log = logs / "issue-1.log"
    log.write_text(codex_event(10, future) + "\n")
    assert round(progress.quota(logs, NOW)["codex"][0]) == 10
    with log.open("a") as stream:
        stream.write(codex_event(20, future))  # still being written: no newline yet
    assert round(progress.quota(logs, NOW)["codex"][0]) == 20
    with log.open("a") as stream:
        stream.write("\n" + codex_event(30, future) + "\n")
    assert round(progress.quota(logs, NOW)["codex"][0]) == 30
    # Rewritten in place with a longer file: the earlier offset no longer applies.
    log.write_text(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "unifiedWindows": {"five_hour": {"utilization": 0.07, "resetsAt": future}}}}) + "\n" + "y" * 4000 + "\n")
    assert set(progress.quota(logs, NOW)) == {"claude"}
    log.unlink()
    assert progress.quota(logs, NOW) == {}


def test_appended_output_does_not_make_an_older_reading_newer(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    future = int((NOW + timedelta(hours=3)).timestamp())

    def reading(percent, minutes):
        return json.dumps({"timestamp": (NOW - timedelta(minutes=minutes)).isoformat(), "type": "event_msg",
                           "payload": {"type": "token_count", "rate_limits": {
                               "primary": {"used_percent": percent, "resets_at": future}, "secondary": None}}})

    older, newer = logs / "issue-1.log", logs / "issue-2.log"
    older.write_text(reading(10, 30) + "\n")
    newer.write_text(reading(67, 10) + "\n")
    os.utime(newer, (100, 100))
    with older.open("a") as stream:
        stream.write("ordinary output from the still-running worker\n" * 50)
    os.utime(older, (200, 200))  # the log with the older reading was written last
    assert round(progress.quota(logs, NOW)["codex"][0]) == 67
    # A genuinely newer reading appended later still wins, wherever it lands.
    with older.open("a") as stream:
        stream.write(reading(80, 1) + "\n")
    assert round(progress.quota(logs, NOW)["codex"][0]) == 80


def claude_reading(five_hour, five_reset, weekly, weekly_reset):
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
        "five_hour": {"utilization": five_hour, "resetsAt": five_reset},
        "seven_day": {"utilization": weekly, "resetsAt": weekly_reset}}}})


def test_expired_peak_in_one_window_does_not_hide_a_newer_reading_of_it(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    old_reset = int((NOW - timedelta(hours=1)).timestamp())
    new_reset = int((NOW + timedelta(hours=4)).timestamp())
    weekly = int((NOW + timedelta(days=3)).timestamp())
    old = claude_reading(0.9, old_reset, 0.10, weekly)
    new = claude_reading(0.2, new_reset, 0.12, weekly)
    # Same weekly reset: only the five-hour window tells which reading is newer.
    (logs / "issue-1.log").write_text(new + "\n" + old + "\n")
    assert progress.quota(logs, NOW) == {"claude": (20.0, datetime.fromtimestamp(new_reset, UTC))}
    (logs / "issue-1.log").write_text(new + "\n")
    (logs / "issue-2.log").write_text(old + "\n")
    assert progress.quota(logs, NOW) == {"claude": (20.0, datetime.fromtimestamp(new_reset, UTC))}


def test_each_window_keeps_its_own_newest_reading_by_event_time(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    old_reset = int((NOW - timedelta(minutes=5)).timestamp())
    new_reset = int((NOW + timedelta(hours=4)).timestamp())
    weekly = int((NOW + timedelta(days=3)).timestamp())

    def reading(minutes, primary, primary_reset, secondary):
        return json.dumps({"timestamp": (NOW - timedelta(minutes=minutes)).isoformat(), "type": "event_msg",
                           "payload": {"type": "token_count", "rate_limits": {
                               "primary": {"used_percent": primary, "resets_at": primary_reset},
                               "secondary": {"used_percent": secondary, "resets_at": weekly}}}})

    (logs / "issue-1.log").write_text(reading(2, 15, new_reset, 31) + "\n")
    (logs / "issue-2.log").write_text(reading(60, 95, old_reset, 30) + "\n")
    assert progress.quota(logs, NOW) == {"codex": (31.0, datetime.fromtimestamp(weekly, UTC))}


def test_merged_work_keeps_each_merged_run_and_ends_at_the_finish_not_last_progress(tmp_path):
    from foragent.state import StateStore

    clock = [NOW - timedelta(hours=30)]
    store = StateStore(tmp_path / "repo", now=lambda: clock[0])
    # One reopened issue merged three times; progress stops being recorded long
    # before each merge (as in a review-only merge), so the finish time is the end.
    for hours in (2, 4, 6):
        store.start_run(7, branch="issue-7", worktree="unused")
        clock[0] += timedelta(minutes=5)
        store.update_run(7, last_progress_at=clock[0].isoformat())
        clock[0] += timedelta(hours=hours) - timedelta(minutes=5)
        store.finish_run(7, "merged")
        clock[0] += timedelta(hours=1)
    work = progress.merged_work(store)
    assert [(number, size, (end - start) / timedelta(hours=1)) for number, size, start, end in work] == [
        (7, "m", 2), (7, "m", 4), (7, "m", 6)]
    samples = progress.learned([store], NOW)
    assert progress.expected(samples["m"], "m")[1:] == (4 * 3600.0, 5 * 3600.0, 3)


def _refresh(store, tracker, prs):
    store.refresh_local_issues(tracker.list_issues("all"), prs, {}, NOW, complete_prs=True)


def test_outside_merges_survive_newest_first_listings_and_reopening(tmp_path):
    from foragent.state import StateStore
    from foragent.tracker import FakeTracker, PullRequest

    clock = [NOW - timedelta(hours=20)]
    store = StateStore(tmp_path / "repo", now=lambda: clock[0])
    tracker = FakeTracker()
    tracker.add_issue(7, "Reopened work", labels=["approved", "size:l"])
    store.start_run(7, branch="foragent/issue-7", worktree="unused")
    first = PullRequest(20, "foragent/issue-7", "main", "First", "", (NOW - timedelta(hours=16)).isoformat())
    clock[0] = NOW - timedelta(hours=16)
    store.finish_run(7, "cancelled")  # the PR was merged by hand; the run ended without a gate merge
    clock[0] = NOW - timedelta(hours=10)
    store.start_run(7, branch="foragent/issue-7", worktree="unused")  # work restarted after a reopening
    second = PullRequest(21, "foragent/issue-7", "main", "Second", "", (NOW - timedelta(hours=2)).isoformat())
    tracker.close_issue(7)
    # GitHub lists merged PRs newest first: the older merge must not win.
    _refresh(store, tracker, [second, first])
    assert store.local_issues()[0]["merged_at"] == second.merged_at
    assert sorted((end - start) / timedelta(hours=1) for *_, start, end in progress.merged_work(store)) == [4, 8]
    # Reopening the issue keeps both earlier merges, even once the listing drops the older PR.
    tracker.issues[7] = replace(tracker.issues[7], state="open")
    _refresh(store, tracker, [second])
    assert len(progress.merged_work(store)) == 2


def test_merge_samples_keep_the_facts_seen_at_the_merge(tmp_path):
    from foragent.state import StateStore
    from foragent.tracker import FakeTracker, PullRequest

    clock = [NOW - timedelta(hours=20)]
    store = StateStore(tmp_path / "repo", now=lambda: clock[0])
    tracker = FakeTracker()
    # An unapproved issue merged by hand, then reopened, approved and closed again.
    tracker.add_issue(7, "Unapproved work", labels=["size:l"])
    store.start_run(7, branch="foragent/issue-7", worktree="unused")
    merged = PullRequest(20, "foragent/issue-7", "main", "By hand", "", (NOW - timedelta(hours=16)).isoformat())
    tracker.close_issue(7)
    # An approved issue merged by hand, then relabelled to another size.
    tracker.add_issue(8, "Approved work", labels=["approved", "size:l"])
    store.start_run(8, branch="foragent/issue-8", worktree="unused")
    relabelled = PullRequest(21, "foragent/issue-8", "main", "By hand", "", (NOW - timedelta(hours=12)).isoformat())
    tracker.close_issue(8)
    # A gate merge whose issue is relabelled afterwards.
    tracker.add_issue(9, "Gate work", labels=["approved", "size:s"])
    store.start_run(9, branch="foragent/issue-9", worktree="unused")
    clock[0] = NOW - timedelta(hours=18)
    store.finish_run(9, "merged")
    _refresh(store, tracker, [merged, relabelled])
    before = sorted(progress.merged_work(store))
    assert [(number, size) for number, size, *_ in before] == [(8, "l"), (9, "s")]
    for number, labels in ((7, ["approved", "size:l"]), (8, ["approved", "size:s"]), (9, ["approved", "size:l"])):
        tracker.issues[number] = replace(tracker.issues[number], state="open", labels=frozenset(labels))
        _refresh(store, tracker, [merged, relabelled])
        tracker.close_issue(number)
    _refresh(store, tracker, [merged, relabelled])
    assert sorted(progress.merged_work(store)) == before


def test_issue_listed_before_the_merge_does_not_record_it_as_unclosed(tmp_path):
    from foragent.state import StateStore
    from foragent.tracker import FakeTracker, PullRequest

    clock = [NOW - timedelta(hours=6)]
    store = StateStore(tmp_path / "repo", now=lambda: clock[0])
    tracker = FakeTracker()
    tracker.add_issue(7, "Raced work", labels=["approved", "size:l"])
    tracker.issues[7] = replace(tracker.issues[7], updated_at=clock[0].isoformat())
    store.start_run(7, branch="foragent/issue-7", worktree="unused")
    stale = tracker.list_issues("all")  # listed while the PR is still open
    clock[0] = NOW - timedelta(hours=2)
    merged = PullRequest(20, "foragent/issue-7", "main", "By hand", "", clock[0].isoformat())
    tracker.issues[7] = replace(tracker.issues[7], state="closed", updated_at=clock[0].isoformat())
    store.refresh_local_issues(stale, [merged], {}, NOW, complete_prs=True)
    assert progress.merged_work(store) == []  # undecided, not recorded as open
    _refresh(store, tracker, [merged])
    assert [(number, (end - start) / timedelta(hours=1)) for number, _, start, end in progress.merged_work(store)] == [
        (7, 4)]
    # Once decided, a reopening does not undo it.
    tracker.issues[7] = replace(tracker.issues[7], state="open", updated_at=NOW.isoformat())
    _refresh(store, tracker, [merged])
    assert len(progress.merged_work(store)) == 1
