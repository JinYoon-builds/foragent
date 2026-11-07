"""The owner serializes orchestrator writes; producers keep bytes without Git publication."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from threading import Event, current_thread
import fcntl

import pytest
import yaml

from conftest import ACCEPTANCE, git_command
from foragent.scheduler import TickReport
from foragent import (brief, lanes, main_write_requests as writes, merge_owner, orch_close,
                      orch_inbox, orch_role_memory, order_flush, order_flush_journal, order_notes,
                      roles_commands, status)
from foragent.config import Config
from foragent.git import GitError
from foragent.state import StateStore
from test_order import offline_publication  # noqa: F401

NOW = datetime(2026, 11, 6, tzinfo=UTC)
MEMORY = ".claude/agent-memory/fa-docs/MEMORY.md"


@pytest.fixture
def owner(offline_publication, tmp_path):
    root, config, tracker, git, calls, origin = offline_publication
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    ctx = SimpleNamespace(repo="owner/repo", repo_root=root, config=replace(config, merge_owner=True),
                          tracker=tracker, git=git, store=store, now=lambda: NOW)
    return SimpleNamespace(ctx=ctx, root=root, git=git, store=store, origin=origin, calls=calls)


def contents(case, name):
    return git_command(case.origin, "show", f"main:{name}")


def capture(case, name, data):
    path = case.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return writes.capture(case.git, case.ctx.config, [name], "Memory", "lane docs", store=case.store)


def drain(case):
    report = TickReport()
    writes.drain(case.ctx, report)
    return report.actions


def test_fifo_repeated_capture_and_restart_are_once(owner):
    case = owner
    head = case.git.head_sha()
    first = capture(case, MEMORY, b"First\n")
    assert capture(case, MEMORY, b"First\n") is None
    second = capture(case, MEMORY, b"First\nSecond\n")
    assert [r["id"] for r in writes.pending(case.store)] == [first["id"], second["id"]]
    assert case.git.head_sha() == head and not any(a[0] == "push" for a in case.calls)
    assert git_command(case.root, "diff", "--cached", "--name-only") == ""
    assert len(drain(case)) == 2
    landed = git_command(case.origin, "rev-parse", "main")
    assert contents(case, MEMORY) == "First\nSecond"
    case.ctx.store = StateStore(case.store.root)
    assert drain(case) == []
    assert git_command(case.origin, "rev-parse", "main") == landed
    assert case.git.head_sha() == head  # Owner writes never disturb the producer checkout.


@pytest.mark.parametrize("name", ["src/setting.py", "docs/ideas.md", "docs/rules.md", "docs/decisions.md"])
def test_open_batch_defers_and_current_main_is_preserved(owner, git_history, name):
    case = owner
    capture(case, name, b"VALUE = 1\n")
    base = git_command(case.origin, "rev-parse", "main")
    train = f"refs/heads/foragent/batch-merge-{base[:8]}-1"
    git_command(case.origin, "update-ref", train, base)
    assert drain(case) == [] and writes.pending(case.store)
    git_history.update({"upstream.txt": "Batch shipped\n"})
    git_command(case.origin, "update-ref", "-d", train)
    assert drain(case)
    assert contents(case, "upstream.txt") == "Batch shipped"
    assert contents(case, name) == "VALUE = 1"
    assert git_command(case.origin, "rev-parse", "main^") == git_history.tip


def test_conflict_is_retained_and_reported_with_lane(owner, git_history):
    case = owner
    request = capture(case, MEMORY, b"Local lesson\n")
    git_history.update({MEMORY: "Different main lesson\n"})
    assert "blocked" in " ".join(drain(case))
    row, = writes.pending(case.store)
    assert row["status"] == "blocked" and row["payload"] == request["payload"]
    message, = orch_inbox.requests(case.store)
    assert "lane docs" in message["text"] and message["main_write"] == row["id"]
    assert contents(case, MEMORY) == "Different main lesson"
    assert drain(case) == [] and len(orch_inbox.requests(case.store)) == 1


def test_receipt_recovers_lost_push_response(owner, monkeypatch):
    case = owner
    capture(case, MEMORY, b"Retained\n")
    original = case.git._call

    def lost(args):
        result = original(args)
        if args[0] == "push":
            raise GitError("response lost")
        return result

    monkeypatch.setattr(case.git, "_call", lost)
    assert "pending" in " ".join(drain(case))
    landed = git_command(case.origin, "rev-parse", "main")
    monkeypatch.setattr(case.git, "_call", original)
    assert drain(case) and not writes.pending(case.store)
    assert git_command(case.origin, "rev-parse", "main") == landed
    assert sum(args[0] == "push" for args in case.calls) == 1


def test_conflict_notification_recovers_after_interruption(owner, git_history, monkeypatch):
    capture(owner, MEMORY, b"Local lesson\n")
    git_history.update({MEMORY: "Different main lesson\n"})

    def interrupted(*args, **kwargs):
        raise OSError("interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(orch_inbox, "add_request", interrupted)
        with pytest.raises(OSError, match="interrupted"):
            drain(owner)
    assert writes.pending(owner.store)[0]["status"] == "blocked"
    drain(owner)
    drain(owner)
    assert len(orch_inbox.requests(owner.store)) == 1


@pytest.mark.parametrize("name, neutral", [(MEMORY, True), ("docs/decisions.md", False)])
def test_only_memory_remains_d111_neutral(owner, name, neutral):
    from foragent.run_base_move import BaseMove, move_is_neutral

    before = owner.git.head_sha()
    capture(owner, name, b"Learned\n")
    drain(owner)
    owner.git.fetch("origin", "main")
    after = owner.git.ref_sha("origin/main")
    assert move_is_neutral(owner.git, BaseMove(before, after), ctx=owner.ctx, n=1,
                           own_locked=(), strict=True) is neutral


def test_queued_creation_then_deletion_is_retained(owner):
    path = owner.root / MEMORY
    capture(owner, MEMORY, b"Temporary lesson\n")
    path.unlink()
    writes.capture(owner.git, owner.ctx.config, [str(Path(MEMORY).parent)], "Remove lesson", "role docs",
                   store=owner.store)
    assert len(writes.pending(owner.store)) == 2
    drain(owner)
    assert not writes.pending(owner.store)
    assert MEMORY not in git_command(owner.origin, "ls-tree", "-r", "--name-only", "main").splitlines()


@pytest.mark.parametrize("change", ["unchanged", "edit", "delete"])
def test_queued_file_baseline_survives_unrelated_head_advance(owner, git_history, change):
    first = capture(owner, MEMORY, b"First lesson\n")
    before = owner.git.head_sha()
    git_history.update({"unrelated.txt": "Main advanced\n"})
    owner.git.fetch("origin", "main")
    git_command(owner.root, "merge", "--ff-only", "origin/main")
    assert owner.git.head_sha() != before

    path = owner.root / MEMORY
    if change == "delete":
        path.unlink()
    elif change == "edit":
        path.write_bytes(b"Edited lesson\n")
    second = writes.capture(owner.git, owner.ctx.config, [str(path.parent.relative_to(owner.root))],
                            "Memory", "lane docs", store=owner.store)
    if change == "unchanged":
        assert second is None
    else:
        snapshot = second["payload"]["files"][MEMORY]
        assert snapshot["before"] == first["payload"]["files"][MEMORY]["after"]
        assert snapshot["after"] == (None if change == "delete" else writes._content(b"Edited lesson\n"))
    assert len(drain(owner)) == (1 if change == "unchanged" else 2)
    assert not writes.pending(owner.store)
    assert contents(owner, "unrelated.txt") == "Main advanced"
    if change == "delete":
        assert MEMORY not in git_command(owner.origin, "ls-tree", "-r", "--name-only", "main").splitlines()
    else:
        assert contents(owner, MEMORY) == ("Edited lesson" if change == "edit" else "First lesson")


@pytest.mark.parametrize("name", ["carriage\rreturn.md", "line\nbreak.md", "tab\tand \\\"quote.md", "새-교훈.md"])
def test_owner_retains_literal_memory_names_and_bytes(owner, name):
    path = str(Path(MEMORY).parent / name)
    data = b"Binary lesson\x00\xff\r\n"
    capture(owner, path, data)
    drain(owner)
    owner.git.fetch("origin", "main")
    owner.git._call(["update-ref", "refs/heads/main", owner.git.ref_sha("origin/main")])
    assert writes._tracked(owner.git, [path])[path]["bytes"] == writes._content(data)["bytes"]
    assert writes.capture(owner.git, owner.ctx.config, [path], "Memory", "role docs", store=owner.store) is None


def test_request_json_preserves_non_utf8_names(owner):
    name = b"raw-\xff.md".decode("utf-8", "surrogateescape")
    payload = {"kind": "files", "files": {name: {"before": None, "after": writes._content(b"Lesson\xff")}}}
    writes.submit(owner.store, "main", "Memory", "role docs", payload)
    assert writes.pending(owner.store)[0]["payload"] == payload


@pytest.mark.parametrize("changed", [False, True])
def test_close_locks_apply_later_or_report_conflict(git_history, tmp_path, monkeypatch, changed):
    from helpers.orch_close import OWN, base_files, base_tip, close, superseded_case

    case = superseded_case(git_history, tmp_path, monkeypatch)
    case.ctx.config = replace(case.ctx.config, merge_owner=True)
    before = base_tip(case)
    assert close(case) == 0
    assert base_tip(case) == before and OWN in base_files(case)
    request, = writes.pending(case.store)
    if changed:
        git_history.update({OWN: "def test_replacement():\n    assert True\n"})
    writes.drain(case.ctx, TickReport())
    if changed:
        assert writes.pending(case.store)[0]["status"] == "blocked"
        assert OWN in base_files(case)
        message, = orch_inbox.requests(case.store)
        assert request["requester"] in message["text"]
    else:
        assert not writes.pending(case.store) and OWN not in base_files(case)
        landed = base_tip(case)
        writes.drain(case.ctx, TickReport())
        assert base_tip(case) == landed


def test_close_locks_preserve_executable_target_mode(owner):
    base = owner.git.head_sha()
    blob = owner.git.blob_sha(base, ACCEPTANCE)
    title = "Executable acceptance lock"
    git_command(owner.origin, "fast-import", "--quiet", input=(
        f"commit refs/heads/main\ncommitter Tests <tests@example.com> {int(NOW.timestamp())} +0000\n"
        f"data {len(title)}\n{title}\nfrom {base}\nM 100755 {blob} {ACCEPTANCE}\n\ndone\n"))
    assert git_command(owner.origin, "ls-tree", "main", "--", ACCEPTANCE).startswith("100755 blob ")
    writes.submit(owner.store, "main", "Remove lock", "issue #1",
                  {"kind": "close-locks", "n": 1, "paths": {ACCEPTANCE: blob}})
    capture(owner, MEMORY, b"Later memory\n")

    assert len(drain(owner)) == 2
    assert not writes.pending(owner.store)
    assert ACCEPTANCE not in git_command(owner.origin, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert contents(owner, MEMORY) == "Later memory"
    assert owner.git.head_sha() == base
    assert not orch_inbox.requests(owner.store)


def test_notes_flush_queues_once_without_touching_checkout_or_push(owner, monkeypatch):
    case = owner
    order_notes.append(case.store, kind="decision", boundary="chosen", content="Retained decision")
    original = case.git.head_sha()
    for _ in range(2):
        result = order_flush.flush(case.root, case.ctx.config, case.ctx.tracker, case.git, case.store,
                                   repo=case.ctx.repo, boundary="chosen")
        assert result["status"] == "waiting"
    assert len(writes.pending(case.store)) == 1
    assert case.git.head_sha() == original and not (case.root / "docs/decisions.md").exists()
    assert not any(a[0] == "push" for a in case.calls)
    finish = writes._finish

    def finish_without_git_locks(ctx, row):
        # A concurrent flush can hold flush.lock while waiting for this lock.
        with (case.git.common_dir() / "foragent-publish.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finish(ctx, row)

    monkeypatch.setattr(writes, "_finish", finish_without_git_locks)
    drain(case)
    assert contents(case, "docs/decisions.md") == "- Retained decision"
    assert order_flush_journal.published_entries(case.store)
    assert order_flush.flush(case.root, case.ctx.config, case.ctx.tracker, case.git, case.store,
                             repo=case.ctx.repo, boundary="chosen")["status"] == "empty"


def test_bad_rule_is_reported_and_retained(owner):
    order_notes.append(owner.store, kind="rule", boundary="chosen", content="New rule", replace="Missing rule")
    order_flush.flush(owner.root, owner.ctx.config, owner.ctx.tracker, owner.git, owner.store,
                      repo=owner.ctx.repo, boundary="chosen")
    assert "blocked" in " ".join(drain(owner))
    assert writes.pending(owner.store)[0]["payload"]["notes"][0]["text"] == "New rule"


class WriterGit:
    """Producer fake: any Git publication is a test failure."""

    def __init__(self, root):
        self.root = root

    def _call(self, args):
        if args[:1] == ["symbolic-ref"]:
            return "main"
        if args[:3] == ["rev-parse", "--verify", "--quiet"]:
            raise GitError("No remote-tracking ref")
        assert args[:2] == ["--literal-pathspecs", "ls-tree"], args
        return ""

    def head_sha(self):
        return "head"

    def current_branch(self):
        return "main"


@pytest.mark.parametrize("writer", ["memory", "lane", "role-add", "role-set", "close"])
def test_each_writer_records_without_committing_or_pushing(tmp_path, monkeypatch, writer):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".foragent.yml").write_text("repo: owner/repo\nmerge_owner: true\n"
                                      "test_command: uv run pytest -q\nskill_libraries: []\n")
    git = WriterGit(root)
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    monkeypatch.setattr(writes, "store_for", lambda *a: store)
    monkeypatch.setattr(writes, "_tracked", lambda *a: {})
    monkeypatch.setattr(roles_commands._Edit, "store", lambda self: store)
    if writer == "lane":
        lanes.add_lane(root, "new", ["src/**"], git=git)
    elif writer.startswith("role-"):
        roles_commands.add_role(root, "new", skills=[], scope=["docs/**"], signals=[], git=git)
        if writer == "role-set":
            roles_commands.set_role(root, "new", scope=["src/**"], git=git)
    elif writer == "memory":
        folder = root / MEMORY
        folder.parent.mkdir(parents=True)
        folder.write_text("Role lesson\n")
        config = Config(merge_owner=True, roles={"docs": {"memory": str(Path(MEMORY).parent), "skills": []}})
        orch_role_memory.publish(SimpleNamespace(config=config, git=git, repo_root=root, store=store), TickReport())
    else:
        approval = SimpleNamespace(acceptance_blobs={"tests/acceptance/test_old.py": "blob"})
        monkeypatch.setattr(store, "load_approval", lambda n: approval)
        ctx = SimpleNamespace(config=Config(merge_owner=True), git=git, store=store)
        orch_close._remove_locks(ctx, 1, 2, set())
    rows = writes.pending(store)
    assert len(rows) == (2 if writer == "role-set" else 1)
    assert all(row["requester"] for row in rows)
    if writer != "close":
        assert rows[-1]["payload"]["files"]


def test_pending_visible_in_status_and_brief(owner, capsys):
    from foragent.commands.gate import _print_status

    request = capture(owner, MEMORY, b"Lesson\n")
    gate = status.collect(owner.store, owner.ctx.config, NOW)
    for view in (gate, brief.collect(owner.store, owner.ctx.config, NOW)):
        row, = view["main_writes"]
        assert row["id"] == request["id"] and row["requester"] == "lane docs"
        assert "payload" not in row
    _print_status({**gate, "launchd": {"loaded": False}})
    assert request["id"] in capsys.readouterr().out


@pytest.mark.parametrize("enabled", [False, True])
def test_main_write_alone_starts_the_owner_when_enabled(owner, monkeypatch, enabled):
    capture(owner, MEMORY, b"Lesson\n")
    owner.ctx.config = replace(owner.ctx.config, merge_owner=enabled)
    started = []
    monkeypatch.setattr(merge_owner.subprocess, "Popen", lambda *a, **kw: started.append(a))
    merge_owner.start(owner.ctx, TickReport())
    assert bool(started) == enabled
    assert len(writes.pending(owner.store)) == 1


def test_dream_merge_queues_before_checks_without_merging(owner, monkeypatch):
    from foragent import orch

    monkeypatch.setattr(orch, "_open_pr", lambda *a: SimpleNamespace(head="foragent/dream-docs", base="main"))
    monkeypatch.setattr(orch, "_check_dream_inputs", lambda *a: None)
    monkeypatch.setattr(orch, "_premerge", lambda *a: pytest.fail("owner performs verification later"))
    monkeypatch.setattr(owner.ctx.tracker, "pr_head_sha", lambda *a: "dream-head")
    assert "queued" in orch.merge(owner.ctx, 42, approved_by="human")
    row, = writes.pending(owner.store)
    assert row["payload"]["kind"] == "dream-merge" and row["requester"] == "dream lane docs"
    assert not any(a[0] == "push" for a in owner.calls)


def test_owner_drains_before_preparing_a_batch(owner, monkeypatch):
    from foragent import merge_owner_batch, orch_queue, run_merge_turn

    events = []
    monkeypatch.setattr(writes, "drain", lambda *a: events.append("writes"))
    monkeypatch.setattr(merge_owner, "admissions", lambda *a: {})
    monkeypatch.setattr(run_merge_turn, "_rows", lambda *a: [])
    monkeypatch.setattr(orch_queue, "get_global", lambda *a: [])
    monkeypatch.setattr(merge_owner_batch, "prepare", lambda *a: events.append("batch"))
    owner.ctx.config = replace(owner.ctx.config, merge_batch=True)
    merge_owner._tick(owner.ctx, TickReport())
    assert events == ["writes", "batch"]


def test_snapshot_is_read_after_acquiring_its_queue_baseline(owner, monkeypatch):
    """An older producer waiting to submit must not reverse a newer shared config."""
    path = owner.root / ".foragent.yml"
    entered, proceed = Event(), Event()
    lock = owner.store._state_lock

    @contextmanager
    def delayed_lock():
        if current_thread().name.startswith("lane"):
            entered.set()
            assert proceed.wait(5)
        with lock():
            yield

    monkeypatch.setattr(owner.store, "_state_lock", delayed_lock)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="lane") as workers:
        lane = workers.submit(writes.capture, owner.git, owner.ctx.config,
                              [".foragent.yml"], "Lane", "lane api", store=owner.store)
        try:
            assert entered.wait(5)
            data = yaml.safe_load(path.read_text())
            data["roles"] = {"new": {"scope": ["docs/**"]}}
            path.write_text(yaml.safe_dump(data))
            writes.capture(owner.git, owner.ctx.config, [".foragent.yml"], "Role", "role new", store=owner.store)
        finally:
            proceed.set()
        lane.result(timeout=5)
    drain(owner)
    assert not writes.pending(owner.store)
    assert yaml.safe_load(contents(owner, ".foragent.yml"))["roles"] == data["roles"]


@pytest.mark.parametrize("multiple", [False, True], ids=["pushurl", "multiple-pushurls"])
def test_reservation_at_any_push_destination_defers_the_request(owner, tmp_path, monkeypatch, multiple):
    destination = tmp_path / "push.git"
    git_command(tmp_path, "clone", "--bare", str(owner.origin), str(destination))
    if multiple:
        git_command(owner.root, "remote", "set-url", "--add", "--push", "origin", str(owner.origin))
    git_command(owner.root, "remote", "set-url", "--add", "--push", "origin", str(destination))
    base = git_command(destination, "rev-parse", "main")
    train = f"refs/heads/foragent/batch-merge-{base[:8]}-1"
    git_command(destination, "update-ref", train, base)
    original = owner.git._call
    pushes = []

    def transport(args):
        if args[0] == "push":
            pushes.append(args)
            for remote in original(["remote", "get-url", "--push", "--all", "origin"]).splitlines():
                git_command(Path(remote), "fetch", str(owner.root), args[-1])
            return ""
        return original(args)

    monkeypatch.setattr(owner.git, "_call", transport)
    capture(owner, "src/setting.py", b"VALUE = 1\n")
    assert drain(owner) == []
    assert not pushes and writes.pending(owner.store)[0]["status"] == "pending"
    assert git_command(destination, "rev-parse", "main") == base
    git_command(destination, "update-ref", "-d", train)
    assert drain(owner) and not writes.pending(owner.store)
    assert git_command(destination, "show", "main:src/setting.py") == "VALUE = 1"


def test_dream_wait_for_merge_turn_stays_retryable_and_unblocks_later_writes(owner, monkeypatch):
    from foragent import orch, run_merge_turn

    tracker = owner.ctx.tracker
    pr = tracker.create_pr(head="foragent/dream-docs", base="main", title="Memory", body="")
    tracker.set_pr_head_sha(pr, "dream-head")
    writes.submit(owner.store, "main", "Dream", "dream lane docs",
                  {"kind": "dream-merge", "pr": pr, "head": "dream-head",
                   "approved_by": "human", "at": NOW.isoformat()})
    capture(owner, MEMORY, b"Later memory\n")
    monkeypatch.setattr(run_merge_turn, "claim", lambda *a: 99)
    assert "waits for the merge turn" in " ".join(drain(owner))
    assert [row["status"] for row in writes.pending(owner.store)] == ["pending", "pending"]
    assert not orch_inbox.requests(owner.store)

    def merge_after_holder_finishes(ctx, number, **kwargs):
        assert run_merge_turn.claim(ctx, None, "dream-head") is None
        tracker.merge_pr(number, expected_head_sha="dream-head")

    monkeypatch.setattr(run_merge_turn, "claim", lambda *a: None)
    monkeypatch.setattr(orch, "merge", merge_after_holder_finishes)
    assert len(drain(owner)) == 2
    assert not writes.pending(owner.store)
    assert tracker.merged_prs == [pr]
    assert contents(owner, MEMORY) == "Later memory"


@pytest.mark.parametrize("changed", [None, "head", "target"])
def test_closed_dream_is_retired_and_releases_fifo(owner, monkeypatch, changed):
    from foragent import orch

    tracker = owner.ctx.tracker
    pr = tracker.create_pr(head="foragent/dream-docs", base="main", title="Memory", body="")
    tracker.set_pr_head_sha(pr, "queued-head")
    request = writes.submit(owner.store, "main", "Dream", "dream lane docs",
                            {"kind": "dream-merge", "pr": pr, "head": "queued-head",
                             "approved_by": "human", "at": NOW.isoformat()})
    if changed:
        if changed == "head":
            tracker.set_pr_head_sha(pr, "replacement-head")
        else:
            tracker.update_pr(pr, base="other", title="Memory", body="")
        drain(owner)
        assert writes.pending(owner.store)[0]["status"] == "blocked"
    tracker.close_pr(pr)
    capture(owner, MEMORY, b"Later memory\n")
    monkeypatch.setattr(orch, "merge", lambda *a, **kw: pytest.fail("closed dreams cannot merge"))

    assert len(drain(owner)) == 2
    retired, applied = writes.requests(owner.store)
    assert retired["id"] == request["id"] and retired["status"] == "retired"
    assert "closed without merging" in retired["reason"] and "landed" not in retired
    assert applied["status"] == "done" and not writes.pending(owner.store)
    assert contents(owner, MEMORY) == "Later memory"
    assert not tracker.merged_prs
    owner.ctx.store = StateStore(owner.store.root)
    assert drain(owner) == []


@pytest.mark.parametrize("changed", ["head", "target", None])
def test_already_merged_dream_must_match_queued_head_and_target(owner, changed):
    tracker = owner.ctx.tracker
    pr = tracker.create_pr(head="foragent/dream-docs", base="main", title="Memory", body="")
    tracker.set_pr_head_sha(pr, "queued-head")
    writes.submit(owner.store, "main", "Dream", "dream lane docs",
                  {"kind": "dream-merge", "pr": pr, "head": "queued-head",
                   "approved_by": "human", "at": NOW.isoformat()})
    if changed == "target":
        tracker.update_pr(pr, base="other", title="Memory", body="")
    if changed == "head":
        tracker.set_pr_head_sha(pr, "replacement-head")
    tracker.merge_pr(pr, expected_head_sha=tracker.pr_head_sha(pr))
    before = git_command(owner.origin, "rev-parse", "main")
    drain(owner)
    row, = writes.requests(owner.store)
    assert row["status"] == ("blocked" if changed else "done")
    if changed:
        assert "head or target changed" in row["error"]
        assert "landed" not in row
    assert git_command(owner.origin, "rev-parse", "main") == before


def test_role_added_and_removed_before_drain_stays_removed(owner, monkeypatch):
    path = owner.root / ".foragent.yml"
    config = yaml.safe_load(path.read_text())
    config.update(repo="owner/repo", merge_owner=True, skill_libraries=[], test_command="uv run pytest")
    path.write_text(yaml.safe_dump(config))
    monkeypatch.setattr(roles_commands._Edit, "store", lambda self: owner.store)
    before = owner.git.head_sha()
    roles_commands.add_role(owner.root, "temporary", skills=[], scope=["docs/**"], signals=[], git=owner.git)
    roles_commands.remove_role(owner.root, "temporary", git=owner.git)
    assert owner.git.head_sha() == before
    assert len(writes.pending(owner.store)) == 2
    drain(owner)
    assert not writes.pending(owner.store)
    assert "temporary" not in yaml.safe_load(contents(owner, ".foragent.yml"))["roles"]
    names = git_command(owner.origin, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert not any("fa-temporary" in name for name in names)


def test_notes_can_be_retained_without_local_remote_tracking_history(owner):
    git_command(owner.root, "update-ref", "-d", "refs/remotes/origin/main")
    order_notes.append(owner.store, kind="decision", boundary="chosen", content="Offline decision")
    result = order_flush.flush(owner.root, owner.ctx.config, owner.ctx.tracker, owner.git, owner.store,
                               repo=owner.ctx.repo, boundary="chosen")
    assert result["status"] == "waiting"
    assert len(writes.pending(owner.store)) == 1
    drain(owner)
    assert not writes.pending(owner.store)
    assert contents(owner, "docs/decisions.md") == "- Offline decision"


def test_one_pass_lost_push_recovers_every_request_from_one_receipt(owner, monkeypatch):
    """A batch commit names each request; a lost push response lands none twice."""
    case = owner
    first = capture(case, MEMORY, b"Docs\n")
    second = writes.capture(case.git, case.ctx.config, [MEMORY], "Memory", "lane docs", store=case.store)
    assert second is None
    other = case.root / ".claude/agent-memory/fa-orch/MEMORY.new.md"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_bytes(b"Orch\n")
    third = writes.capture(case.git, case.ctx.config, [str(other.relative_to(case.root))], "Orch", "lane orch",
                           store=case.store)
    before = git_command(case.origin, "rev-parse", "main")
    original = case.git._call

    def lost(args):
        result = original(args)
        if args[0] == "push":
            raise GitError("response lost")
        return result

    monkeypatch.setattr(case.git, "_call", lost)
    actions = drain(case)
    assert [a for a in actions if "pending" in a] and not [a for a in actions if "applied" in a]
    assert [row["status"] for row in writes.pending(case.store)] == ["pending", "pending"]
    monkeypatch.setattr(case.git, "_call", original)
    assert len(drain(case)) == 2 and not writes.pending(case.store)
    assert git_command(case.origin, "rev-list", "--count", f"{before}..main") == "1"
    message = git_command(case.origin, "show", "-s", "--format=%B", "main")
    assert f"{writes.TRAILER}: {first['id']}" in message and f"{writes.TRAILER}: {third['id']}" in message
    assert sum(args[0] == "push" for args in case.calls) == 1
