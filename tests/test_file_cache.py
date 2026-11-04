"""File versions, shared reader paths and fresh data after append/replace."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
import json
import os
from pathlib import Path
from threading import Barrier

import pytest

from foragent import file_cache, merge_owner, orch_events, run_merge_turn as queue, watch_data, watch_lane_facts
from foragent.state import Run, StateStore


NOW = datetime(2026, 11, 4, tzinfo=UTC)


def count_parses(monkeypatch):
    parsed = []
    loads = json.loads

    def observed(text, *args, **kwargs):
        value = loads(text, *args, **kwargs)
        if isinstance(value, dict) and value.get("cache_probe"):
            parsed.append(value)
        return value

    monkeypatch.setattr(json, "loads", observed)
    return parsed


def rewrite(path, content):
    before = path.stat()
    temporary = path.with_suffix(".replacement")
    temporary.write_text(content)
    # Same size and mtime force the reader to notice the changed inode.
    assert temporary.stat().st_size == before.st_size
    os.utime(temporary, ns=(before.st_atime_ns, before.st_mtime_ns))
    temporary.replace(path)
    assert path.stat().st_ino != before.st_ino


def test_queue_readers_share_cache_across_stores_and_refresh_after_append_and_rewrite(tmp_path, monkeypatch):
    store = StateStore(tmp_path, now=lambda: NOW)
    store.start_run(1, branch="one", worktree="one")
    store.start_run(2, branch="two", worktree="two")
    path = tmp_path / queue.QUEUE
    row = {"n": 1, "event": "ready", "target": "main", "started_at": NOW.isoformat(),
           "merge_owner": True, "publication": {}, "cache_probe": True}
    queue._append(store, row)
    parsed = count_parses(monkeypatch)
    place = queue.Seat(1, NOW.isoformat(), "main")
    for _ in range(3):
        peer = StateStore(tmp_path)
        assert queue.holds_turn(peer, place)
        assert merge_owner.pending(peer, 1)
        assert len(merge_owner.admissions(peer)) == 1
    assert len(parsed) == 1
    with store._state_lock():
        queue._append(store, {**row, "n": 2})
    assert [row["n"] for row in queue._rows(store)] == [1, 2]
    assert len(parsed) == 3
    lines = path.read_text().splitlines(keepends=True)
    rewrite(path, "".join(reversed(lines)))
    assert [row["n"] for row in queue._rows(store)] == [2, 1]
    assert len(parsed) == 5
    assert not queue.holds_turn(store, place)
    assert len(parsed) == 5


def test_queue_cache_refreshes_after_in_place_rewrite_with_restored_mtime(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    path = tmp_path / queue.QUEUE
    row = {"n": 1, "event": "ready", "target": "main", "started_at": NOW.isoformat(),
           "merge_owner": True, "publication": {}, "cache_probe": True}
    path.write_text(json.dumps(row) + "\n")
    parsed = count_parses(monkeypatch)
    assert list(merge_owner.admissions(store)) == [(1, row["started_at"])]
    before = path.stat()
    with path.open("r+") as stream:
        stream.write(json.dumps({**row, "n": 2}) + "\n")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    assert after.st_ctime_ns != before.st_ctime_ns
    for peer in (store, StateStore(tmp_path)):
        assert list(merge_owner.admissions(peer)) == [(2, row["started_at"])]
    assert len(parsed) == 2


def test_history_readers_share_parse_and_return_detached_runs(tmp_path, monkeypatch):
    path = tmp_path / "runs.jsonl"
    first = {**asdict(Run(1, "branch", "worktree", None, NOW.isoformat(), NOW.isoformat())),
             "finished_at": NOW.isoformat(), "cache_probe": True,
             "owner_merge_started_at": NOW.isoformat(),
             "owner_merge": {"merged_at": NOW.isoformat(), "pr": 17, "target": "main", "head": "head"},
             "pause_regions": {"wait": [NOW.isoformat(), NOW.isoformat()]}}
    path.write_text(json.dumps(first) + "\n")
    parsed = count_parses(monkeypatch)
    for _ in range(3):
        store = StateStore(tmp_path)
        assert store.read_history()[0].number == 1
        assert store.finish_times() == {(1, NOW.isoformat()): NOW.isoformat()}
        assert store.merge_times() == {(1, NOW.isoformat()): NOW.isoformat()}
        assert store.owner_merge_facts() == {(1, NOW.isoformat()): first["owner_merge"]}
    assert len(parsed) == 1
    store.read_history()[0].pause_regions["wait"].append("caller mutation")
    assert len(store.read_history()[0].pause_regions["wait"]) == 2
    store.owner_merge_facts()[1, NOW.isoformat()]["target"] = "caller mutation"
    assert store.owner_merge_facts()[1, NOW.isoformat()]["target"] == "main"
    with path.open("a") as stream:
        stream.write(json.dumps({**first, "number": 2}) + "\n")
    assert [run.number for run in store.read_history()] == [1, 2]
    assert len(parsed) == 3
    lines = path.read_text().splitlines(keepends=True)
    rewrite(path, "".join(reversed(lines)))
    assert [run.number for run in store.read_history()] == [2, 1]
    assert len(parsed) == 5


def test_dashboard_event_readers_share_parse_and_observe_append_and_rewrite(tmp_path, monkeypatch):
    path = tmp_path / "orch-events.jsonl"
    event = {"issue": 1, "kind": "phase", "phase": "review", "summary": "first", "say": "hi",
             "at": NOW.isoformat(), "cache_probe": True}
    path.write_text(json.dumps(event) + "\n")
    parsed = count_parses(monkeypatch)
    for _ in range(3):
        store = StateStore(tmp_path)
        assert watch_data.recent_events(store)[0]["summary"] == "first"
        assert watch_data.journal_facts(store)[0][1][0]["phase"] == "review"
        assert len(watch_lane_facts.journal(store)) == 1
        assert store.talk_events(1) == []
        assert len(orch_events.events(store, "phase")) == 1
        assert orch_events.acknowledged_at(store, 1) is None
        assert orch_events.has_event(store, 1, "phase")
    assert len(parsed) == 1
    watch_data.recent_events(store)[0]["summary"] = "caller mutation"
    watch_data.journal_facts(store)[0][1][0]["phase"] = "caller mutation"
    orch_events.events(store, "phase")[0]["summary"] = "caller mutation"
    assert watch_data.recent_events(store)[0]["summary"] == "first"
    with path.open("a") as stream:
        stream.write(json.dumps({**event, "kind": "talk-opened", "summary": "later"}) + "\n")
    assert watch_data.recent_events(store)[0]["summary"] == "later"
    assert len(store.talk_events(1)) == 1
    assert len(parsed) == 3
    replacement = path.read_text().replace("talk-opened", "talk-closed")
    rewrite(path, replacement)
    assert store.talk_events(1)[0]["kind"] == "talk-closed"
    assert len(parsed) == 5


@pytest.mark.parametrize("tolerant", [False, True])
def test_cache_uses_open_file_version_when_replaced_after_stat(tmp_path, monkeypatch, tolerant):
    path = tmp_path / "journal.jsonl"
    path.write_text('{"cache_probe": true, "value": 1}\n')
    parsed = count_parses(monkeypatch)
    open_file = Path.open
    replaced = []

    def open_after_replace(self, *args, **kwargs):
        if self == path and not replaced:
            replaced.append(True)
            temporary = path.with_suffix(".new")
            with open_file(temporary, "w") as stream:
                stream.write('{"cache_probe": true, "value": 2}\n')
            temporary.replace(path)
        return open_file(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_after_replace)
    assert file_cache.read_jsonl(path, tolerant=tolerant)[0]["value"] == 2
    assert file_cache.read_jsonl(path, tolerant=tolerant)[0]["value"] == 2
    assert len(parsed) == 1


def test_concurrent_misses_parse_once_and_share_symlinked_roots(tmp_path, monkeypatch):
    path = tmp_path / "journal.jsonl"
    path.write_text('{"cache_probe": true}\n')
    alias = tmp_path / "alias.jsonl"
    alias.symlink_to(path)
    parsed = count_parses(monkeypatch)
    barrier = Barrier(6)

    def read(index):
        barrier.wait()
        return file_cache.read_jsonl(path if index % 2 else alias)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(read, range(6)))
    assert all(result == [{"cache_probe": True}] for result in results)
    assert len(parsed) == 1


def test_tolerant_append_recovers_incomplete_line_and_strict_history_still_raises(tmp_path):
    path = tmp_path / "journal.jsonl"
    assert file_cache.read_jsonl(path) == []
    path.write_text('{"n": 1}\n[]\n{"n":')
    assert file_cache.read_jsonl(path, tolerant=True) == [{"n": 1}]
    with pytest.raises(ValueError):
        file_cache.read_jsonl(path)
    with path.open("a") as stream:
        stream.write(" 2}\n")
    assert file_cache.read_jsonl(path, tolerant=True) == [{"n": 1}, {"n": 2}]
    path.unlink()
    assert file_cache.read_jsonl(path, tolerant=True) == []


def test_reader_policies_share_one_parse_even_after_a_strict_error(tmp_path, monkeypatch):
    path = tmp_path / 'orch-events.jsonl'
    path.write_bytes(b'{"cache_probe": true, "text": "\xff"}\n[]\nmalformed\n')
    parsed = count_parses(monkeypatch)
    assert file_cache.read_jsonl(path, tolerant=True, errors='replace') == [
        {'cache_probe': True, 'text': '\ufffd'}]
    with pytest.raises(UnicodeDecodeError):
        file_cache.read_jsonl(path, tolerant=True)
    with pytest.raises(ValueError):
        file_cache.read_jsonl(path, errors='replace')
    assert file_cache.read_jsonl(path, tolerant=True, errors='replace')[0]['text'] == '\ufffd'
    assert len(parsed) == 1


def test_json_readers_share_one_value_and_see_restored_mtime_changes(tmp_path, monkeypatch):
    from foragent import gates

    store = StateStore(tmp_path)
    path = tmp_path / 'issues.json'
    path.write_text('{"issues": {}, "cache_probe": true, "revision": 1}')
    parsed = count_parses(monkeypatch)
    assert store.issues_revision() == 1
    assert gates._read_json(path)['revision'] == 1
    assert len(parsed) == 1
    before = path.stat()
    path.write_text(path.read_text().replace('"revision": 1', '"revision": 2'))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert gates._read_json(path)['revision'] == 2
    assert StateStore(tmp_path).issues_revision() == 2
    assert len(parsed) == 2


def test_notice_tail_reuses_lines_and_preserves_byte_cut_and_error_numbers(tmp_path, monkeypatch):
    from foragent import orch_inbox

    store = StateStore(tmp_path, now=lambda: NOW)
    path = tmp_path / 'orch-events.jsonl'
    event = {'at': NOW.isoformat(), 'kind': 'notification', 'summary': '한글\u2028tail',
             'status': 'sent', 'cache_probe': True}
    # Use the actual notice kind; a literal Unicode separator remains inside one LF record.
    event['kind'] = orch_inbox.NOTICE_KIND
    last = json.dumps(event, ensure_ascii=False).encode() + b'\n'
    first = b'{"kind":"note","padding":"xxxxxxxxxxxxxxxx"}\n'
    path.write_bytes(first + b'broken\n' + last)
    parsed = count_parses(monkeypatch)
    monkeypatch.setattr(orch_inbox, 'NOTICE_TAIL_BYTES', len(last) + len(b'broken\n') + 2)
    file_cache.read_jsonl(path, tolerant=True, errors='replace')
    notices, problems = orch_inbox.recent_notices(store, NOW.replace(hour=1))
    assert notices == []  # The record is before the requested window.
    assert problems == ['orch-events.jsonl: tail line 1 unreadable (JSONDecodeError)']
    notices, problems = orch_inbox.recent_notices(store, NOW.replace(year=2025))
    assert notices == [event]
    assert problems[0].startswith('orch-events.jsonl: only the last ')
    notices[0]['summary'] = 'caller mutation'
    assert orch_inbox.recent_notices(store, NOW.replace(year=2025))[0] == [event]
    assert len(parsed) == 1
    # A cut exactly after LF retains the next complete line, too.
    monkeypatch.setattr(orch_inbox, 'NOTICE_TAIL_BYTES', len(last))
    assert orch_inbox.recent_notices(store, NOW.replace(year=2025))[0] == [event]


def test_temporary_facts_reads_release_values_when_their_files_disappear(tmp_path):
    import weakref

    path = tmp_path / 'orch-events.jsonl'
    path.write_text('{"summary":"snapshot"}\n')
    # A weak reference observes retained parsed objects without depending on the cache's shape.
    class Value(dict):
        pass
    with file_cache.temporary_reads(tmp_path):
        value = file_cache.read(path, lambda data: Value(json.loads(data)))
        reference = weakref.ref(value)
        assert file_cache.read(path, lambda data: pytest.fail('parsed twice')) is value
    del value
    assert reference() is None


def test_transcript_raw_selection_keeps_escaped_and_unrelated_bad_rows_out(tmp_path, monkeypatch):
    from foragent import orch_refresh

    path = tmp_path / 'transcript.jsonl'
    event = {'type': 'system', 'subtype': 'compact_boundary', 'sessionId': 's', 'cache_probe': True}
    row = json.dumps(event)
    # The old transcript reader selects the literal word before parsing JSON.
    path.write_text('[' * 2000 + '\n' + row.replace('compact_boundary', 'compact_\\u0062oundary') + '\n'
                    + row + '\n')
    parsed = count_parses(monkeypatch)
    assert orch_refresh.compactions(path, 's') == 1
    assert orch_refresh.compactions(path, 's') == 1
    assert len(parsed) == 2
