"""Publication checks hand off durable intents without holding a file lock."""

from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
import json
import os
from types import SimpleNamespace

import pytest

from foragent import capacity, order_publications, order_publications_process as checks, state_claims
from foragent.config import Config
from foragent.state import StateStore


@pytest.fixture
def context(tmp_path):
    store = StateStore(tmp_path / "state")
    store._write_json(store.root / "order-publications/request.json",
                      {"id": "request", "status": "pending", "sequence": 1})
    return SimpleNamespace(repo="owner/repo", repo_root=tmp_path, store=store,
                           config=Config(merge_owner=True), now=lambda: datetime.now(UTC))


def test_launcher_transfers_independent_intent_without_holding_a_lock(context, monkeypatch):
    children = []
    context.committed_config = True
    monkeypatch.setenv(capacity.TEST_SLOT_ENV, "held")

    def spawn(argv, **options):
        assert argv[1:3] == ["-m", "foragent.order_publications_process"]
        request = json.loads(argv[3])
        assert request["config"]["merge_owner"] is True
        assert request["committed_config"] is True
        assert request["store_root"] == str(context.store.root.resolve())
        token = argv[4]
        assert "pass_fds" not in options and options["start_new_session"]
        assert capacity.TEST_SLOT_ENV not in options["env"]
        lock = context.store.lock_acceptance_baseline(checks.LOCK, blocking=False)
        assert lock is not None, "The launcher must release its file lock before Popen"
        lock.close()
        children.append(token)
        return SimpleNamespace(pid=os.getpid())

    monkeypatch.setattr(checks.subprocess, "Popen", spawn)
    report = SimpleNamespace(actions=[])
    owner_lock = context.store.lock_acceptance_baseline("merge-owner", blocking=False)
    try:
        checks.start(context, report)
        checks.start(context, report)
        assert len(children) == 1
        assert report.actions == ["order publication checks started"]
        assert state_claims.acquire(context.store.root / f"{checks.LOCK}.intent") is None
    finally:
        owner_lock.close()
        for token in children:
            state_claims.release(context.store.root / f"{checks.LOCK}.intent", token)
    lock = context.store.lock_acceptance_baseline(checks.LOCK, blocking=False)
    assert lock is not None
    lock.close()


def test_process_refreshes_origin_configuration_before_checking(context, monkeypatch):
    from foragent import git, tracker

    context.config = replace(context.config, executor="claude")
    events = []

    def fetch(*args):
        events.append("fetch")

    def blob_sha(ref, path):
        assert events == ["fetch"]
        assert (ref, path) == ("origin/main", ".foragent.yml")
        return "config-blob"

    monkeypatch.setattr(git, "GitRepo", lambda root: SimpleNamespace(
        root=root, fetch=fetch, blob_sha=blob_sha,
        _call=lambda args: "executor: codex\nmerge_owner: true\ntest_command: python -m pytest\n"))
    monkeypatch.setattr(tracker, "GhTracker", lambda repo: SimpleNamespace(repo=repo))

    def check_pending(ctx, report):
        assert ctx.committed_config is True
        assert ctx.config.executor == "codex"
        events.append("checked")

    monkeypatch.setattr(order_publications, "check_pending", check_pending)
    request = {"repo": context.repo, "repo_root": str(context.repo_root),
               "store_root": str(context.store.root), "config": asdict(context.config),
               "committed_config": True}
    token = state_claims.acquire(context.store.root / f"{checks.LOCK}.intent")
    checks.run_process(request, token)
    assert events == ["fetch", "checked"]


def test_launch_failure_releases_reservation_for_next_tick(context, monkeypatch):
    def spawn(*args, **kwargs):
        raise OSError("cannot start child")

    monkeypatch.setattr(checks.subprocess, "Popen", spawn)
    report = SimpleNamespace(actions=[])
    checks.start(context, report)
    checks.start(context, report)
    assert report.actions == ["order publication checks start deferred: cannot start child"] * 2
    assert order_publications.pending(context.store)[0]["status"] == "pending"


@pytest.mark.parametrize("held", ["owner-off", "pm-held", "project-stop", "github-backoff"])
def test_launcher_preserves_existing_admission_holds(context, monkeypatch, held):
    if held == "owner-off":
        context.config = replace(context.config, merge_owner=False)
    elif held == "pm-held":
        context.store._write_json(context.store.root / "order-publications/request.json",
                                  {"id": "request", "status": "pm-held", "sequence": 1})
    elif held == "project-stop":
        monkeypatch.setattr(checks.orch_stop, "active", lambda store: "Stopped")
    else:
        context.store.pause_github(context.now() + timedelta(hours=1))
    monkeypatch.setattr(checks.subprocess, "Popen", lambda *a, **kw: pytest.fail("Started held work"))
    report = SimpleNamespace(actions=[])
    checks.start(context, report)
    assert not report.actions


@pytest.mark.parametrize("transport", ["intent", "descriptor"])
def test_process_checks_pending_without_reserving_merge_owner(context, monkeypatch, transport):
    from foragent import git, tracker

    monkeypatch.setattr(git, "GitRepo", lambda root: SimpleNamespace(root=root, fetch=lambda *args: None))
    monkeypatch.setattr(tracker, "GhTracker", lambda repo: SimpleNamespace(repo=repo))
    seen = []

    def check_pending(ctx, report):
        seen.append(ctx.repo)
        assert state_claims.acquire(ctx.store.root / f"{checks.LOCK}.intent") is None
        lock = ctx.store.lock_acceptance_baseline(checks.LOCK, blocking=False)
        assert lock is not None, "The red checker must run outside its file lock"
        lock.close()
        owner_lock = ctx.store.lock_acceptance_baseline("merge-owner", blocking=False)
        assert owner_lock is not None
        owner_lock.close()

    monkeypatch.setattr(order_publications, "check_pending", check_pending, raising=False)
    request = {"repo": context.repo, "repo_root": str(context.repo_root),
               "store_root": str(context.store.root), "config": asdict(context.config)}
    if transport == "intent":
        reservation = state_claims.acquire(context.store.root / f"{checks.LOCK}.intent")
    else:
        lock = context.store.lock_acceptance_baseline(checks.LOCK, blocking=False)
        reservation = os.dup(lock.fileno())
        lock.close()
    checks.run_process(request, reservation)
    assert seen == [context.repo]
    lock = context.store.lock_acceptance_baseline(checks.LOCK, blocking=False)
    assert lock is not None
    lock.close()
