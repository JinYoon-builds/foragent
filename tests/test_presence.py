import io
import re

import pytest
import yaml

from foragent import statusline


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv(statusline.NESTED_ENV, raising=False)
    root = tmp_path / "project"
    root.mkdir()
    (root / ".foragent.yml").write_text(yaml.safe_dump({"repo": "offline/no-state"}), encoding="utf-8")
    monkeypatch.chdir(root)
    return root


def refresh(previous):
    statusline._write_json(statusline.record_path(), {"enabled": True, "previous_command": previous})
    out = io.BytesIO()
    code = statusline.run(io.BytesIO(b"{}"), out)
    return code, out.getvalue().decode("utf-8")


@pytest.mark.parametrize("previous", [None, "", "fa statusline"])
def test_presence_shows_without_a_previous_statusline(project, previous):
    assert refresh(previous) == (0, "🐿 0 lanes working · ✋ 0 · 0 merged today")


def test_project_without_local_state_shows_zero_counts_after_user_output(project):
    assert refresh("printf mine") == (0, "mine  🐿 0 lanes working · ✋ 0 · 0 merged today")


def test_nested_wrapper_adds_nothing(project, monkeypatch):
    monkeypatch.setenv(statusline.NESTED_ENV, "1")
    assert refresh("printf mine") == (0, "")


def test_presence_off_with_no_previous_statusline_prints_nothing(project):
    (project / ".foragent.yml").write_text(
        yaml.safe_dump({"repo": "offline/no-state", "presence": {"statusline": False}}), encoding="utf-8")
    assert refresh(None) == (0, "")


def test_a_failed_rerun_does_not_count_the_earlier_merge_again(tmp_path):
    from datetime import UTC, datetime, timedelta

    from foragent import presence
    from foragent.state import StateStore

    now = datetime(2026, 10, 27, 12, tzinfo=UTC)
    clock = [now - timedelta(hours=3)]
    store = StateStore(tmp_path / "state", lambda: clock[0])
    store.start_run(7, branch="foragent/issue-7", worktree=tmp_path / "w7", lane="api")
    clock[0] = now - timedelta(hours=2)
    store.record_merge(7, clock[0])
    store.finish_run(7, outcome="merged")
    assert presence.counts(store, now)[2] == 1
    clock[0] = now - timedelta(hours=1)
    store.start_run(7, branch="foragent/issue-7", worktree=tmp_path / "w7", lane="api")
    clock[0] = now - timedelta(minutes=30)
    store.finish_run(7, outcome="checks-failed")
    assert presence.counts(store, now) == (0, 0, 1)


def test_a_gate_merge_counts_before_its_run_is_finished_and_only_once(tmp_path):
    from datetime import UTC, datetime, timedelta

    from foragent import presence
    from foragent.state import StateStore

    now = datetime(2026, 10, 27, 12, tzinfo=UTC)
    clock = [now - timedelta(hours=2)]
    store = StateStore(tmp_path / "state", lambda: clock[0])
    store.start_run(8, branch="foragent/issue-8", worktree=tmp_path / "w8", lane="api")
    clock[0] = now - timedelta(hours=1)
    store.record_merge(8, clock[0])
    # The gate still has GitHub calls to make before finish_run moves the merge into history.
    assert presence.counts(store, now) == (1, 0, 1)
    store.finish_run(8, outcome="merged")
    assert presence.counts(store, now) == (0, 0, 1)


def test_without_a_repo_key_a_non_github_origin_shows_zeros(project):
    (project / ".foragent.yml").write_text(yaml.safe_dump({"language": "en"}), encoding="utf-8")
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text('[remote "origin"]\n\turl = https://gitlab.com/o/n.git\n',
                                            encoding="utf-8")
    assert refresh(None) == (0, "🐿 0 lanes working · ✋ 0 · 0 merged today")


def test_the_repo_key_wins_over_origin(project, monkeypatch):
    from foragent import presence
    from foragent.state import StateStore

    (project / ".git").mkdir()
    (project / ".git" / "config").write_text('[remote "origin"]\n\turl = git@github.com:o/origin.git\n',
                                            encoding="utf-8")
    seen = []
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo: seen.append(repo) or cls(project)))
    presence.statusline_segment(project, statusline.datetime.now(statusline.UTC))
    assert seen == ["offline/no-state"]


def test_project_without_a_repository_still_gets_its_first_minute_squirrel(project):
    """No repo key and no GitHub origin: the hook still records the start; the squirrel shows."""
    import json
    from datetime import datetime, timedelta

    from foragent import presence

    (project / ".foragent.yml").write_text(yaml.safe_dump({"language": "en"}), encoding="utf-8")
    out = io.BytesIO()
    payload = json.dumps({"session_id": "no-repo", "cwd": str(project)}).encode()
    assert statusline.session_start(io.BytesIO(payload), out) == 0
    message = json.loads(out.getvalue())["systemMessage"]
    assert message == "since the last report: 0 merges · ✋ 0 · /fa for the report"
    started = datetime.fromisoformat(json.loads(
        presence._session_path("no-repo").read_text(encoding="utf-8"))["started_at"])
    assert presence.squirrel(project, "no-repo", started + timedelta(seconds=59)) is not None
    assert presence.squirrel(project, "no-repo", started + timedelta(seconds=60)) is None
    assert presence.squirrel(project, "other-session", started) is None


STALL_BASE = "🐿 0 lanes working · ✋ 0 · 0 merged today"


@pytest.fixture
def stall_store(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from foragent.state import StateStore

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("FORAGENT_ROLE", raising=False)
    root = tmp_path / "project"
    root.mkdir()
    (root / ".foragent.yml").write_text(yaml.safe_dump({"repo": "offline/stall"}), encoding="utf-8")
    now = datetime(2026, 10, 30, 12, tzinfo=UTC)
    return root, StateStore.for_repo("offline/stall", now=lambda: now), now


def _stalled_row(store, now, notes):
    import json
    from datetime import timedelta

    from foragent import orch_inbox

    row = orch_inbox.add_request(store, "planning", "opaque")
    path = store.root / "inbox" / "requests" / f"{row['id']}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(status="processing", created_at=(now - timedelta(minutes=10)).isoformat(), notes=notes)
    path.write_text(json.dumps(record), encoding="utf-8")


def _stall_hours(store, now):
    from datetime import timedelta

    from foragent import orch_inbox, presence

    stuck, _unreadable = presence.stalled(orch_inbox.scan(store, "requests")[0], now)
    return [(now - since) // timedelta(hours=1) for _row, since in stuck]


def test_a_malformed_note_is_ignored_on_its_own(stall_store):
    from datetime import timedelta

    root, store, now = stall_store
    at = (now - timedelta(hours=2)).isoformat()
    _stalled_row(store, now, ["junk", {"status": "processing"}, {"at": at},
                              {"status": "processing", "at": at}, {"at": now.isoformat()}])
    assert _stall_hours(store, now) == [2]


def test_the_request_status_decides_even_after_a_note_naming_another_status(stall_store):
    from datetime import timedelta

    root, store, now = stall_store
    _stalled_row(store, now, [{"status": "processing", "at": (now - timedelta(hours=4)).isoformat()},
                              {"status": "done", "at": (now - timedelta(hours=3)).isoformat()}])
    assert _stall_hours(store, now) == [4]


def test_an_unreadable_requests_box_leaves_out_only_the_stall_fact(stall_store):
    from datetime import timedelta

    from foragent import orch_inbox, presence

    root, store, now = stall_store
    _stalled_row(store, now, [{"status": "processing", "at": (now - timedelta(hours=4)).isoformat()}])
    store.start_run(7, branch="foragent/issue-7", worktree=store.worktree_path(7), lane="main")
    (store.root / "inbox" / "requests" / "r9.json").write_text("{not json", encoding="utf-8")
    assert presence.statusline_segment(root, now) == "🐿 1 lanes working · ✋ 0 · 0 merged today"
    # The corrupt file is left out alone and named; the valid stall stays.
    assert _stall_hours(store, now) == [4]
    assert orch_inbox.scan(store, "requests")[1] == ['"inbox/requests/r9.json": unreadable (JSONDecodeError)']


def test_statusline_counts_open_questions_only_in_the_planning_session(stall_store, monkeypatch):
    from foragent import orch_inbox, presence

    root, store, now = stall_store
    assert presence.statusline_segment(root, now) == STALL_BASE
    first = orch_inbox.ask(store, "One?", ["a", "b"], 1)
    # An open question with no issue is the person's: every session names the recipient,
    # only the planning session adds where to answer.
    assert presence.statusline_segment(root, now) == (
        STALL_BASE + " · questions: you 1 · 1 question needs your answer in the planning session")
    orch_inbox.ask(store, "Two?", ["a", "b"], 1)
    assert presence.statusline_segment(root, now).endswith(
        " · questions: you 2 · 2 questions need your answer in the planning session")
    orch_inbox.answer(store, first["id"], "a")
    assert presence.statusline_segment(root, now).endswith(
        " · questions: you 1 · 1 question needs your answer in the planning session")
    monkeypatch.setenv("FORAGENT_ROLE", "orchestrator")
    assert presence.statusline_segment(root, now) == STALL_BASE + " · questions: you 1"


def test_prompt_context_carries_recent_notices_and_names_unreadable_ones(stall_store):
    import json
    from datetime import timedelta

    from foragent import orch_inbox, presence

    root, store, now = stall_store
    (root / ".git").mkdir()
    (root / ".git/config").write_text('[remote "origin"]\n\turl = https://github.com/offline/stall.git\n')
    store.root.mkdir(parents=True, exist_ok=True)
    assert presence.bind_checkout(root, now) is True
    assert presence.bind_checkout(root, now) is False, "the first binding stays"
    assert presence.prompt_context(root, now) == (None, [])
    store._now = lambda: now - timedelta(hours=25)
    orch_inbox.record_notice(store, "sent", "Too old to show", channel="push", ref="q1")
    store._now = lambda: now - timedelta(minutes=5)
    event = orch_inbox.record_notice(store, "failed", "Push for q2\nfailed", channel="ntfy", ref="q2", issue=7)
    assert {key: event[key] for key in ("kind", "status", "channel", "ref", "issue", "summary")} == {
        "kind": "notice", "status": "failed", "channel": "ntfy", "ref": "q2", "issue": 7,
        "summary": "Push for q2 failed"}
    context, problems = presence.prompt_context(root, now)
    assert problems == []
    assert "Too old to show" not in context
    assert f'- "{event["at"]}" "failed" (channel "ntfy" ref "q2") #7: "Push for q2 failed"' in context
    with (store.root / "orch-events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"kind": "notice", "at": "broken", "summary": "x"}) + "\n")
    context, problems = presence.prompt_context(root, now)
    assert len(problems) == 1 and re.fullmatch(
        r"orch-events\.jsonl: tail line \d+ is not a readable notice \(wrong at, status\)", problems[0]), problems
    assert "Push for q2 failed" in context and "Partial read" in context


@pytest.mark.parametrize("broken", ['{', '[]', '{"seq":"bad"}', '{"id":"q9","seq":9,"status":"open","issues":"7"}'])
def test_a_corrupt_question_file_hides_no_valid_question_count(stall_store, broken):
    from foragent import orch_inbox, presence

    root, store, now = stall_store
    orch_inbox.ask(store, "Valid?", ["a", "b"], 1, issues=[5])
    (store.root / "inbox" / "questions" / "broken.json").write_text(broken, encoding="utf-8")
    # The last case is an object with wrong field types: skipped alone like the others.
    question = " · questions: you 1 · 1 question needs your answer in the planning session"
    assert presence.statusline_segment(root, now) == "🐿 0 lanes working · ✋ 1 · 0 merged today" + question
    assert presence.session_start_message(root, None, now) == (
        "since the last report: 0 merges · ✋ 1 · /fa for the report")


def _bound_stall_store(stall_store):
    from foragent import orch_inbox, presence

    root, store, now = stall_store
    (root / ".git").mkdir()
    (root / ".git/config").write_text('[remote "origin"]\n\turl = https://github.com/offline/stall.git\n')
    store.root.mkdir(parents=True, exist_ok=True)
    assert presence.bind_checkout(root, now)
    from datetime import timedelta

    orch_inbox.ask(store, "Visible question", ["a", "b"], 1)
    _stalled_row(store, now, [{"status": "processing", "at": (now - timedelta(hours=2)).isoformat()}])
    return root, store, now


@pytest.mark.parametrize("kind", ["requests", "questions"])
@pytest.mark.parametrize("damage", ["unlistable", "file-in-place"])
def test_an_unreadable_box_folder_is_a_failed_read_not_an_empty_box(stall_store, kind, damage):
    import os
    import shutil

    from foragent import orch_inbox, presence

    root, store, now = _bound_stall_store(stall_store)
    folder = store.root / "inbox" / kind
    if damage == "unlistable":
        os.chmod(folder, 0)
    else:
        shutil.rmtree(folder)
        folder.write_text("not a folder")
    try:
        rows, problems = orch_inbox.scan(store, kind)
        context, notes = presence.prompt_context(root, now)
    finally:
        if damage == "unlistable":
            os.chmod(folder, 0o755)
    assert rows == [] and len(problems) == 1 and problems[0].startswith(f'"inbox/{kind}": unreadable (')
    assert notes == problems and "Partial read" in context
    other = '"opaque"' if kind == "questions" else "Visible question"
    assert other in context, "the other box still reaches the planning turn"


def test_a_missing_box_folder_is_an_empty_box(stall_store):
    from foragent import orch_inbox

    root, store, now = stall_store
    assert orch_inbox.scan(store, "questions") == ([], [])


def test_a_corrupt_binding_file_is_reported_not_silently_unbound(stall_store):
    from foragent import presence

    root, store, now = _bound_stall_store(stall_store)
    (store.root / presence.BINDING).write_text("{")
    assert presence.prompt_context(root, now) == (None, ["checkout.json: unreadable (JSONDecodeError)"])
    assert presence.bind_checkout(root, now), "the next session start repairs a corrupt binding"
    assert "Visible question" in presence.prompt_context(root, now)[0]


def test_an_unreadable_state_folder_is_reported(stall_store, monkeypatch):
    from foragent import presence

    root, store, now = _bound_stall_store(stall_store)
    real = presence.os.stat

    def denied(path, *args, **kwargs):
        if str(path) == str(store.root):
            raise PermissionError(13, "Permission denied")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(presence.os, "stat", denied)
    assert presence.prompt_context(root, now) == (
        None, ["state folder: unreadable (PermissionError: Permission denied)"])


def _hook(root, monkeypatch):
    """Run the installed prompt hook entry with a strict UTF-8 stderr, as Claude Code's pipe is."""
    import json
    import sys

    from foragent import statusline

    stderr = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
    out = io.BytesIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    assert statusline.prompt_context(io.BytesIO(json.dumps({"cwd": str(root)}).encode()), out) == 0
    stderr.flush()
    error = stderr.buffer.getvalue().decode("utf-8")
    return json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"], error


SURROGATE = "bad\ud800text"


@pytest.mark.parametrize("field", ["request-text", "request-from", "question", "question-option",
                                   "notice-summary", "notice-channel", "filename"])
def test_a_lone_surrogate_in_one_field_replaces_only_that_character(stall_store, monkeypatch, field):
    import json
    from datetime import timedelta

    from foragent import orch_inbox

    root, store, now = _bound_stall_store(stall_store)
    if field.startswith("request"):
        row = orch_inbox.add_request(store, "planning", "placeholder")
        path = store.root / "inbox/requests" / f"{row['id']}.json"
        record = json.loads(path.read_text())
        record.update(status="processing", notes=[], created_at=(now - timedelta(hours=3)).isoformat(),
                      **({"text": SURROGATE} if field == "request-text" else {"from": SURROGATE}))
        path.write_text(json.dumps(record))  # ensure_ascii writes the JSON escape \ud800
    elif field.startswith("question"):
        row = orch_inbox.ask(store, "placeholder", ["fine"], 1)
        path = store.root / "inbox/questions" / f"{row['id']}.json"
        record = json.loads(path.read_text())
        record.update({"question": SURROGATE} if field == "question" else {"options": ["fine", SURROGATE]})
        path.write_text(json.dumps(record))
    elif field.startswith("notice"):
        event = {"at": (now - timedelta(minutes=1)).isoformat(), "issue": None, "kind": "notice",
                 "status": "sent", "summary": SURROGATE if field == "notice-summary" else "Summary",
                 "channel": SURROGATE if field == "notice-channel" else "push", "ref": None}
        with (store.root / "orch-events.jsonl").open("a") as stream:
            stream.write(json.dumps(event) + "\n")
    else:
        real = orch_inbox.os.scandir
        folder = store.root / "inbox" / "questions"

        class Entry:
            name = "bad\udc80.json"

        class Listing(list):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def scandir(path):
            names = list(real(path))
            return Listing(names + [Entry()]) if str(path) == str(folder) else Listing(names)

        monkeypatch.setattr(orch_inbox.os, "scandir", scandir)
    context, error = _hook(root, monkeypatch)
    assert '"opaque"' in context and "Visible question" in context, "valid rows still reach the turn"
    assert "bad?text" in context or "bad?.json" in context, "only the unencodable character is replaced"
    if field == "filename":
        assert "bad?.json" in error


def test_a_row_that_cannot_be_rendered_is_left_out_alone_and_named(stall_store, monkeypatch):
    from foragent import orch_inbox, presence

    root, store, now = _bound_stall_store(stall_store)
    orch_inbox.ask(store, "Unrenderable", ["x"], 1)
    real = presence._quote

    def fragile(value):
        if value == "Unrenderable":
            raise RuntimeError("cannot render")
        return real(value)

    monkeypatch.setattr(presence, "_quote", fragile)
    context, problems = presence.prompt_context(root, now)
    assert "Visible question" in context and '"opaque"' in context
    assert problems == ['question "q2": could not be shown (RuntimeError)']


DEEP = "[" * 10000 + "]" * 10000


@pytest.mark.parametrize("place", ["requests", "questions", "notice"])
def test_a_deeply_nested_row_is_skipped_alone(stall_store, monkeypatch, place):
    from foragent import presence

    root, store, now = _bound_stall_store(stall_store)
    if place == "notice":
        with (store.root / "orch-events.jsonl").open("a") as stream:
            stream.write('{"kind": "notice", "at": ' + DEEP + "}\n")
    else:
        (store.root / "inbox" / place / "deep.json").write_text(DEEP)
    context, error = _hook(root, monkeypatch)
    assert '"opaque"' in context and "Visible question" in context, "valid rows still reach the turn"
    assert "RecursionError" in error or "unreadable notice line" in error, "the skipped row is named"
    assert presence.prompt_context(root, now)[1], "a partial read, not a complete box"


def test_a_deeply_nested_binding_file_is_a_failed_read(stall_store):
    from foragent import presence

    root, store, now = _bound_stall_store(stall_store)
    (store.root / presence.BINDING).write_text(DEEP)
    assert presence.prompt_context(root, now) == (None, ["checkout.json: unreadable (RecursionError)"])


def test_deeply_nested_hook_input_is_no_input(stall_store):
    from foragent import statusline

    out = io.BytesIO()
    assert statusline.prompt_context(io.BytesIO(DEEP.encode()), out) == 0


def test_one_request_whose_time_cannot_be_computed_does_not_stop_the_others(stall_store, monkeypatch):
    from datetime import timedelta

    from foragent import presence

    root, store, now = _bound_stall_store(stall_store)
    real = presence._processing_since
    rows = [{"id": "r8", "status": "processing", "text": "fragile"},
            {"id": "r9", "status": "processing", "created_at": (now - timedelta(hours=2)).isoformat()}]

    def fragile(row):
        if row["id"] == "r8":
            raise RecursionError("deep")
        return real(row)

    monkeypatch.setattr(presence, "_processing_since", fragile)
    stuck, unreadable = presence.stalled(rows, now)
    assert [row["id"] for row, _ in stuck] == ["r9"] and unreadable == ["r8"]


def _journal(store, *events):
    import json

    with (store.root / "orch-events.jsonl").open("a") as stream:
        for event in events:
            stream.write(json.dumps(event) + "\n")


def _notice(at, summary):
    return {"at": at.isoformat(), "issue": None, "kind": "notice", "status": "sent",
            "summary": summary, "channel": None, "ref": None}


def test_a_journal_tail_that_cuts_into_the_window_is_a_partial_read(stall_store, monkeypatch):
    from datetime import timedelta

    from foragent import orch_inbox

    root, store, now = _bound_stall_store(stall_store)
    monkeypatch.setattr(orch_inbox, "NOTICE_TAIL_BYTES", 400)
    since = now - timedelta(hours=24)
    _journal(store, _notice(now - timedelta(hours=5), "Cut off but in the window"),
             *[{"at": (now - timedelta(hours=1)).isoformat(), "kind": "note", "summary": "filler " * 5}] * 6,
             _notice(now - timedelta(minutes=1), "In the tail"))
    notices, problems = orch_inbox.recent_notices(store, since)
    assert [event["summary"] for event in notices] == ["In the tail"]
    assert problems == ["orch-events.jsonl: only the last 400 bytes read; earlier notices in the window may be missing"]


def test_a_journal_tail_that_reaches_past_the_window_is_complete(stall_store, monkeypatch):
    from datetime import timedelta

    from foragent import orch_inbox

    root, store, now = _bound_stall_store(stall_store)
    monkeypatch.setattr(orch_inbox, "NOTICE_TAIL_BYTES", 400)
    _journal(store, *[{"at": (now - timedelta(hours=30)).isoformat(), "kind": "note", "summary": "old " * 10}] * 6,
             _notice(now - timedelta(minutes=1), "In the tail"))
    notices, problems = orch_inbox.recent_notices(store, now - timedelta(hours=24))
    assert [event["summary"] for event in notices] == ["In the tail"] and problems == []


def test_notices_over_the_limit_and_long_text_say_what_was_left_out(stall_store):
    from datetime import timedelta

    from foragent import presence

    root, store, now = _bound_stall_store(stall_store)
    _journal(store, *[_notice(now - timedelta(minutes=30 - index), f"notice {index}") for index in range(12)],
             _notice(now - timedelta(minutes=1), "x" * (presence.CONTEXT_TEXT_LIMIT + 7)))
    context = presence.prompt_context(root, now)[0]
    assert "notice 2" not in context and "notice 3" in context
    assert "3 earlier notices in the window not shown (only the last 10)" in context
    assert "… [7 more characters not shown]" in context


def test_two_checkouts_starting_at_once_bind_only_one(stall_store, monkeypatch):
    import shutil
    import threading

    from foragent import presence

    root, store, now = stall_store
    (root / ".git").mkdir()
    (root / ".git/config").write_text('[remote "origin"]\n\turl = https://github.com/offline/stall.git\n')
    copy = root.parent / "copy"
    shutil.copytree(root, copy)
    store.root.mkdir(parents=True, exist_ok=True)
    real = presence._bound_root
    first_checked, second_done = threading.Event(), threading.Event()

    def slow_check(*args, **kwargs):
        result = real(*args, **kwargs)
        if threading.current_thread().name == "first":
            first_checked.set()
            second_done.wait(0.5)  # without the lock the second binder writes here
        return result

    monkeypatch.setattr(presence, "_bound_root", slow_check)
    results = {}

    def bind(name, folder):
        results[name] = presence.bind_checkout(folder, now)

    first = threading.Thread(target=bind, args=("first", root), name="first")
    first.start()
    assert first_checked.wait(5)
    second = threading.Thread(target=lambda: (bind("second", copy), second_done.set()), name="second")
    second.start()
    first.join(5)
    second.join(5)
    assert results == {"first": True, "second": False}
    assert presence._bound_root(store, "offline/stall") == root.resolve()


def _raw_journal(store, text):
    with (store.root / "orch-events.jsonl").open("a", encoding="utf-8", errors="surrogatepass") as stream:
        stream.write(text)


def test_an_escaped_kind_is_still_a_notice(stall_store):
    from datetime import timedelta

    from foragent import orch_inbox, presence

    root, store, now = _bound_stall_store(stall_store)
    at = (now - timedelta(minutes=2)).isoformat()
    _raw_journal(store, '{"at": "%s", "kind": "not\\u0069ce", "status": "sent", "summary": "Escaped kind"}\n' % at)
    notices, problems = orch_inbox.recent_notices(store, now - timedelta(hours=24))
    assert [event["summary"] for event in notices] == ["Escaped kind"] and problems == []
    assert '"Escaped kind"' in presence.prompt_context(root, now)[0]


def test_a_line_separator_inside_a_field_keeps_the_notice_whole(stall_store, monkeypatch):
    from datetime import timedelta

    from foragent import orch_inbox

    root, store, now = _bound_stall_store(stall_store)
    store._now = lambda: now - timedelta(minutes=2)
    orch_inbox.record_notice(store, "sent", "Para\u2029graph", channel="push\u2028line")
    notices, problems = orch_inbox.recent_notices(store, now - timedelta(hours=24))
    # append_event folds whitespace in summary (U+2029 included); channel keeps U+2028 as is.
    assert [(event["channel"], event["summary"]) for event in notices] == [("push\u2028line", "Para graph")]
    assert problems == []
    context, error = _hook(root, monkeypatch)
    assert '"push\u2028line"' in context and error == ""


def test_a_lone_surrogate_in_a_raw_notice_line_reaches_the_turn_replaced(stall_store, monkeypatch):
    from datetime import timedelta

    root, store, now = _bound_stall_store(stall_store)
    at = (now - timedelta(minutes=2)).isoformat()
    _raw_journal(store, '{"at": "%s", "kind": "notice", "status": "sent", "summary": "a\\ud800b"}\n' % at)
    context, error = _hook(root, monkeypatch)
    assert '"a?b"' in context and '"opaque"' in context


def test_a_truncated_last_line_is_skipped_alone_with_one_note(stall_store):
    from datetime import timedelta

    from foragent import orch_inbox

    root, store, now = _bound_stall_store(stall_store)
    _journal(store, _notice(now - timedelta(minutes=3), "Complete"))
    _raw_journal(store, '{"at": "%s", "kind": "notice", "summ' % (now - timedelta(minutes=1)).isoformat())
    notices, problems = orch_inbox.recent_notices(store, now - timedelta(hours=24))
    assert [event["summary"] for event in notices] == ["Complete"]
    assert len(problems) == 1 and "unreadable (JSONDecodeError)" in problems[0]


@pytest.mark.parametrize("on_boundary", [True, False])
def test_the_tail_keeps_a_line_that_starts_exactly_at_its_edge(stall_store, monkeypatch, on_boundary):
    import json
    from datetime import timedelta

    from foragent import orch_inbox

    root, store, now = _bound_stall_store(stall_store)
    (store.root / "orch-events.jsonl").unlink(missing_ok=True)
    first = json.dumps(_notice(now - timedelta(hours=30), "Before")) + "\n"
    edge = json.dumps(_notice(now - timedelta(minutes=5), "At the edge")) + "\n"
    last = json.dumps(_notice(now - timedelta(minutes=1), "Last")) + "\n"
    _raw_journal(store, first + edge + last)
    tail = len((edge + last).encode()) + (0 if on_boundary else -3)
    monkeypatch.setattr(orch_inbox, "NOTICE_TAIL_BYTES", tail)
    notices, problems = orch_inbox.recent_notices(store, now - timedelta(hours=24))
    summaries = [event["summary"] for event in notices]
    if on_boundary:
        assert summaries == ["At the edge", "Last"]
    else:
        assert summaries == ["Last"]
        assert problems and "earlier notices in the window may be missing" in problems[0]


@pytest.mark.parametrize("line,wrong", [
    ("[]", None),
    ('{"kind": "notice", "at": "AT", "summary": "s", "status": []}', "status"),
    ('{"kind": "notice", "at": "AT", "summary": "s", "status": "sent", "channel": 17}', "channel"),
    ('{"kind": "notice", "at": "AT", "summary": "s", "status": "sent", "ref": {}}', "ref"),
    ('{"kind": "notice", "at": "AT", "summary": "s", "status": "sent", "issue": true}', "issue"),
    ('{"kind": "notice", "at": "AT", "summary": 5, "status": "sent"}', "summary"),
    ('{"kind": "notice", "at": "AT", "summary": "s"}', "status"),
])
def test_a_wrong_typed_journal_line_is_skipped_alone_and_named(stall_store, line, wrong):
    from datetime import timedelta

    from foragent import orch_inbox

    root, store, now = _bound_stall_store(stall_store)
    _journal(store, _notice(now - timedelta(minutes=3), "Valid"))
    _raw_journal(store, line.replace("AT", (now - timedelta(minutes=1)).isoformat()) + "\n")
    notices, problems = orch_inbox.recent_notices(store, now - timedelta(hours=24))
    assert [event["summary"] for event in notices] == ["Valid"]
    assert len(problems) == 1
    assert problems[0].endswith("(not an object)" if wrong is None else f"(wrong {wrong})"), problems


@pytest.mark.parametrize("kind,field,value", [
    ("requests", "text", 7), ("requests", "from", None), ("requests", "notes", "x"),
    ("questions", "question", []), ("questions", "options", ["a", 2]), ("questions", "recommend", "1"),
    ("questions", "kind", 3), ("questions", "issues", ["7"]),
])
def test_a_wrong_typed_box_field_skips_only_that_row_and_names_the_field(stall_store, kind, field, value):
    import json

    from foragent import orch_inbox, presence

    root, store, now = _bound_stall_store(stall_store)
    row = (orch_inbox.add_request(store, "planning", "spare") if kind == "requests"
           else orch_inbox.ask(store, "spare", ["a"], 1))
    path = store.root / "inbox" / kind / f"{row['id']}.json"
    record = json.loads(path.read_text())
    record[field] = value
    path.write_text(json.dumps(record))
    rows, problems = orch_inbox.scan(store, kind)
    assert row["id"] not in [kept["id"] for kept in rows] and len(rows) == 1
    assert problems == [f'"inbox/{kind}/{row["id"]}.json": not a {kind[:-1]} record (wrong {field})']
    context = presence.prompt_context(root, now)[0]
    assert '"opaque"' in context and "Visible question" in context and "Partial read" in context


@pytest.mark.parametrize("line,wrong", [
    ('{"kind": [], "at": "AT", "summary": "s", "issue": null}', "event (wrong kind)"),
    ('{"at": "AT", "summary": "s", "issue": null}', "event (wrong kind)"),
    ('{"kind": "retry", "at": "AT", "summary": 5, "issue": 3}', "event (wrong summary)"),
    ('{"kind": "retry", "at": "AT", "summary": "s", "issue": "3"}', "event (wrong issue)"),
    ('{"kind": "retry", "at": "AT", "summary": "s", "issue": null, "say": 3}', "event (wrong say)"),
    ('{"kind": "retry", "at": "noon", "summary": "s", "issue": null}', "event (wrong at)"),
    ('{"kind": "phase", "summary": "s", "issue": 1}', "event (wrong at)"),
])
def test_any_malformed_journal_event_is_named_beside_a_valid_notice(stall_store, line, wrong):
    from datetime import timedelta

    from foragent import orch_inbox

    root, store, now = _bound_stall_store(stall_store)
    (store.root / "orch-events.jsonl").unlink(missing_ok=True)
    _journal(store, _notice(now - timedelta(minutes=3), "Valid"))
    _raw_journal(store, line.replace("AT", (now - timedelta(minutes=1)).isoformat()) + "\n")
    notices, problems = orch_inbox.recent_notices(store, now - timedelta(hours=24))
    assert [event["summary"] for event in notices] == ["Valid"]
    assert problems == [f"orch-events.jsonl: tail line 2 is not a readable {wrong}"]


def test_well_formed_events_of_other_kinds_are_not_diagnosed(stall_store):
    from datetime import timedelta

    from foragent import orch_inbox
    from foragent.orch_events import append_event

    root, store, now = _bound_stall_store(stall_store)
    append_event(store, now - timedelta(minutes=2), 201, "retry", "Retry", say="Retried.", verdict="ok")
    notices, problems = orch_inbox.recent_notices(store, now - timedelta(hours=24))
    assert notices == [] and problems == []


def test_planning_question_lines_drop_an_earlier_runs_or_a_closed_issues_question(stall_store):
    """Defect class: planning display paths counting open questions the projection no longer awaits."""
    import os
    from datetime import timedelta

    from foragent import orch_inbox, presence
    from foragent.tracker import FakeTracker

    root, store, now = _bound_stall_store(stall_store)  # One open question with no issue: still awaited.
    tracker = FakeTracker(now=lambda: now)
    tracker.add_issue(7, "Storage", labels=["approved", "blocked", "lane:api"])
    store.start_run(7, branch="issue-7", worktree="unused", lane="api")
    store.save_flag(7, "spec-question", flagged_at=now.isoformat())
    store.finish_run(7, outcome="spec-question")
    store.refresh_local_issues(tracker.list_issues("all"), [], {"api": {}}, now)
    asked = orch_inbox.ask(store, "Run 1: which region?", ["a", "b"], 1, issues=[7])
    later = lambda minutes: now + timedelta(minutes=minutes)
    os.utime(store.root / "flags" / "7.json", (now.timestamp(),) * 2)
    assert presence.statusline_segment(root, now).endswith(" · 2 questions need your answer in the planning session")
    assert asked["question"] in presence.prompt_context(root, now)[0]
    store._now = lambda: later(5)
    store.start_run(7, branch="issue-7", worktree="unused", lane="api")  # The retry starts a new episode.
    segment = presence.statusline_segment(root, later(5))
    assert segment.endswith(" · 1 question needs your answer in the planning session"), segment
    assert asked["question"] not in (presence.prompt_context(root, later(5))[0] or "")
    store.finish_run(7, outcome="merged")
    tracker.close_issue(7)  # Merged and closed: still not awaited.
    store.refresh_local_issues(tracker.list_issues("all"), [], {"api": {}}, later(6))
    segment = presence.statusline_segment(root, later(6))
    assert segment.endswith(" · 1 question needs your answer in the planning session"), segment
    assert asked["question"] not in (presence.prompt_context(root, later(6))[0] or "")
