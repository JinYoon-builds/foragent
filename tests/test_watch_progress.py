from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import os
from types import SimpleNamespace

from foragent.config import Config
from foragent.watch_sections import progress

NOW = datetime(2026, 10, 20, 8, tzinfo=UTC)


def run(number, outcome, finished):
    return SimpleNamespace(number=number, outcome=outcome, last_progress_at=finished.isoformat())


def snapshot(**values):
    base = dict(total=0, completed=0, orders=0, eta=None, recent=0, quota={}, captured_at=NOW)
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


def test_recent_merges_count_only_the_last_hour_once_per_issue():
    history = [
        run(1, "merged", NOW - timedelta(minutes=10)),
        run(1, "merged", NOW - timedelta(minutes=5)),
        run(2, "cancelled", NOW - timedelta(minutes=5)),
        run(3, "merged", NOW - timedelta(minutes=61)),
        run(4, "merged", NOW - timedelta(minutes=59)),
    ]
    assert progress.recent_merges(history, NOW) == 2


def test_finish_time_needs_a_rate_and_remaining_work():
    assert progress.finish_time(29, 3, NOW) == NOW + timedelta(hours=29 / 3)
    assert progress.finish_time(29, 0, NOW) is None
    assert progress.finish_time(0, 3, NOW) is None


def test_render_words_in_both_languages():
    eta = NOW + timedelta(minutes=30)
    for language, finish, evidence in (("ko", "쯤 끝나요", "최근 1시간에 2개 끝냈어요"),
                                       ("en", "done around", "2 finished in the last hour")):
        lines = plain(progress.render(snapshot(total=10, completed=4, orders=2, eta=eta, recent=2),
                                      replace(Config(), language=language), None, now=NOW))
        assert "4/10" in lines[0] and "▓" in lines[0]
        assert finish in lines[1] and evidence in lines[2]
    unknown = plain(progress.render(snapshot(total=10, completed=4, orders=1),
                                    replace(Config(), language="ko"), None, now=NOW))
    assert unknown[1] == "끝나는 시각은 아직 몰라요"
    done = plain(progress.render(snapshot(total=3, completed=3, orders=1), Config(), None, now=NOW))
    assert done[1:] == ["all done"]
    assert progress.render(snapshot(), Config(), None, now=NOW) == []


def test_finish_time_on_another_day_names_the_day():
    tomorrow = NOW.astimezone() + timedelta(days=1)
    lines = plain(progress.render(snapshot(total=2, completed=1, orders=1, eta=tomorrow, recent=1),
                                  replace(Config(), language="ko"), None, now=NOW))
    assert lines[1].startswith("내일 ")


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
