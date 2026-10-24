"""Acceptance test for state-records-tolerant. Locked by Foragent; do not edit."""

import ast
from copy import copy, deepcopy
from dataclasses import MISSING, asdict, fields, make_dataclass
from datetime import UTC, datetime, timedelta
from importlib import import_module
import json
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest

from foragent.state import Approval, Flag, InspectState, Run, StateStore
from test_skills_autosync import isolated_home  # noqa: F401


# Public contract: persisted dataclass hydration uses this helper; Config stays strict.
LOADER = "foragent.records.load_record"
NOW = datetime(2026, 10, 24, 12, 50, tzinfo=UTC)
STAMP = NOW.isoformat()
RUN = {
    "number": 7, "branch": "foragent/issue-7", "worktree": "unused-worktree",
    "pid": None, "started_at": STAMP, "last_progress_at": STAMP,
}
APPROVAL = {
    "number": 7, "body": "## Done when\n```sh\ntrue\n```\n", "body_sha": "body-sha",
    "acceptance_blobs": {"tests/acceptance/test_feature.py": "blob-sha"},
    "approved_by": "human", "approved_at": STAMP,
}
FUTURE = {
    "future_record_metadata": {"schema": 99, "nested": [None, False, {"locale": "한국어"}]},
    "future_optional_value": None,
}


@pytest.fixture(autouse=True)
def offline_boundaries(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("State record acceptance tests must not start processes or use the network")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


@pytest.fixture
def store(tmp_path, isolated_home):
    return StateStore(tmp_path / "state", now=lambda: NOW)


def write_json(store, name, value):
    # Seed the writer's wire format directly; do not round-trip through today's class.
    path = store.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_record(label, read):
    try:
        result = read()
    except TypeError as exc:
        pytest.fail(f"{label} must tolerate fields written by a newer version: {exc}")
    assert result is not None, f"{label} silently discarded a valid record with future fields"
    return result


def assert_fields(record, cls, saved):
    assert isinstance(record, cls)
    for item in fields(cls):
        if not item.init:
            continue
        if item.name in saved:
            expected = saved[item.name]
        elif item.default is not MISSING:
            expected = item.default
        elif item.default_factory is not MISSING:
            expected = item.default_factory()
        else:
            pytest.fail(f"Invalid test fixture: required {cls.__name__}.{item.name} is missing")
        assert getattr(record, item.name) == expected, f"{cls.__name__}.{item.name} changed or lost its default"


def load_record():
    try:
        module = import_module("foragent.records")
    except ModuleNotFoundError as exc:
        if exc.name != "foragent.records":
            raise
        pytest.fail(f"Missing public tolerant loader {LOADER}(cls, data)")
    loader = getattr(module, "load_record", None)
    assert callable(loader), f"Missing public tolerant loader {LOADER}(cls, data)"
    return loader


@pytest.mark.parametrize("kind", ["approval", "flag", "inspect"])
def test_state_record_readers_ignore_unknown_fields_and_supply_defaults(store, kind):
    cls, name, saved, read = {
        "approval": (Approval, "approvals/7.json", APPROVAL, lambda: store.load_approval(7)),
        "flag": (Flag, "flags/7.json", {"number": 7, "reason": "spec-question", "flagged_at": STAMP},
                 lambda: store.load_flag(7)),
        "inspect": (InspectState, "inspect.json", {}, store.load_inspect),
    }[kind]
    payload = {**saved, **deepcopy(FUTURE)}
    path = write_json(store, name, payload)
    assert_fields(read_record(kind, read), cls, saved)
    assert read_json(path) == payload, "Reading a future record must not rewrite it"


@pytest.mark.parametrize("operation", ["get_run", "active_runs", "update_run", "finish_run", "read_history"])
def test_reinstalled_run_records_work_through_every_public_reader(store, operation):
    payload = {**RUN, "process_identities": [], **deepcopy(FUTURE)}
    write_json(store, "runs/7.json", payload)
    write_json(store, "runs.jsonl", {**payload, "status": "finished", "outcome": "merged"})
    calls = {
        "get_run": lambda: store.get_run(7),
        "active_runs": store.active_runs,
        "update_run": lambda: store.update_run(7, attempts=2, detail="progress recorded"),
        "finish_run": lambda: store.finish_run(7, "merged", "completed"),
        "read_history": store.read_history,
    }
    result = read_record(operation, calls[operation])
    if operation in {"active_runs", "read_history"}:
        assert len(result) == 1, f"{operation} must return the future record, not skip it"
        result, = result
    expected = dict(payload)
    if operation == "update_run":
        expected.update(attempts=2, detail="progress recorded")
    elif operation == "finish_run":
        expected.update(status="finished", outcome="merged", detail="completed")
    elif operation == "read_history":
        expected.update(status="finished", outcome="merged")
    assert_fields(result, Run, expected)


def test_reader_from_before_process_identities_can_read_reinstalled_runs(store, monkeypatch):
    import foragent.state as state

    # Reproduce the old schema without reinstalling, running a model, or launching a runner.
    legacy_run = make_dataclass("Run", [
        (item.name, item.type, copy(item)) for item in fields(Run)
        if item.name != "process_identities"
    ], frozen=True)
    payload = {**RUN, "process_identities": [{"pid": 999999, "start_time": 123.5}], **deepcopy(FUTURE)}
    path = write_json(store, "runs/7.json", payload)
    write_json(store, "runs.jsonl", {**payload, "status": "finished", "outcome": "merged"})
    monkeypatch.setattr(state, "Run", legacy_run)
    assert_fields(read_record("pre-process_identities get_run", lambda: store.get_run(7)), legacy_run, payload)
    active = read_record("pre-process_identities active_runs", store.active_runs)
    history = read_record("pre-process_identities read_history", store.read_history)
    assert [item.number for item in active] == [7]
    assert [item.outcome for item in history] == ["merged"]
    assert read_json(path) == payload


def test_unknown_run_fields_survive_update_finish_and_reopened_history(store):
    payload = {**RUN, "process_identities": [], **deepcopy(FUTURE)}
    path = write_json(store, "runs/7.json", payload)
    read_record("get_run before update", lambda: store.get_run(7))

    # Reopen the store at every step: preserving only an in-memory record is insufficient.
    updated = read_record("update_run", lambda: StateStore(store.root).update_run(7, attempts=3, detail="진행 중"))
    assert updated.attempts == 3 and updated.detail == "진행 중"
    for key, value in FUTURE.items():
        assert key in read_json(path) and read_json(path)[key] == value, f"update_run lost unknown field {key}"

    # Include an intervening newer writer's addition and replacement, not just the original extras.
    latest = read_json(path)
    latest["future_record_metadata"] = {"schema": 100, "replacement": [1, {"fresh": True}]}
    latest["future_added_between_updates"] = ["keep", None, 0]
    write_json(store, "runs/7.json", latest)
    expected_extras = {key: value for key, value in latest.items() if key not in {item.name for item in fields(Run)}}

    finished = read_record("finish_run", lambda: StateStore(store.root).finish_run(7, "merged", "완료"))
    assert finished.status == "finished" and finished.outcome == "merged" and finished.detail == "완료"
    snapshot = read_json(path)
    journal = [json.loads(line) for line in (store.root / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(journal) == 1
    for saved in (snapshot, journal[0]):
        for key, value in expected_extras.items():
            assert key in saved and saved[key] == value, f"finish_run lost unknown field {key}"
        assert saved["attempts"] == 3 and saved["status"] == "finished"
    assert_fields(StateStore(store.root).get_run(7), Run, snapshot)
    history = read_record("reopened read_history", StateStore(store.root).read_history)
    assert len(history) == 1
    assert_fields(history[0], Run, snapshot)
    assert StateStore(store.root).active_runs() == []


def test_public_loader_supplies_defaults_and_independent_default_factories(store):
    from foragent.inspect_data import PreparedCommand
    from foragent.run_baseline import AcceptanceBaseline, BaselineBuild
    from foragent.tracker import Dependency

    loader = load_record()
    samples = [
        (Run, RUN), (InspectState, {}),
        (PreparedCommand, {"command": "true", "output": "ok", "exit_code": 0}),
        (Dependency, {"number": 2, "state": "open"}),
        (AcceptanceBaseline, {"command": "pytest", "node_ids": ()}),
        (BaselineBuild, {"base_sha": "base", "test_command": "pytest", "excluded": [], "key": "key", "scope": "scope"}),
    ]
    for cls, required in samples:
        for extras in ({}, FUTURE):
            payload = deepcopy({**required, **extras})
            record = loader(cls, payload)
            assert_fields(record, cls, required)
    first, second = loader(Run, RUN), loader(Run, RUN)
    first.attempt_tiers["1"] = "l"
    first.process_identities.append({"pid": 999999, "start_time": 1.0})
    assert second.attempt_tiers == {} and second.process_identities == []
    first_inspect, second_inspect = loader(InspectState, {}), loader(InspectState, {})
    first_inspect.notified_items.append("one")
    assert second_inspect.notified_items == []


def test_prepared_inspection_records_tolerate_future_nested_fields(store):
    from foragent.inspect_data import PreparedCommand, load_prepared

    required = {"command": "true", "output": "준비됨", "exit_code": 0}
    path = write_json(store, "inspect/prepared/7.json", {
        "base_sha": "base", "results": [{**required, **deepcopy(FUTURE)}], **deepcopy(FUTURE),
    })
    before = path.read_bytes()
    prepared = read_record("load_prepared", lambda: load_prepared(store, 7))
    assert prepared.base_sha == "base" and len(prepared.results) == 1
    assert_fields(prepared.results[0], PreparedCommand, required)
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["label_events", "blocked_by"])
def test_confirmed_tracker_cache_records_tolerate_future_fields(store, kind):
    from foragent import scheduler_checks
    from foragent.tracker import Dependency, Issue, LabelEvent

    issue = Issue(7, "Task", "body", frozenset(), "open", updated_at=STAMP)
    cls, saved, read = {
        "label_events": (LabelEvent, {"label": "approved", "action": "labeled", "actor": "human", "created_at": STAMP},
                         scheduler_checks._label_events),
        "blocked_by": (Dependency, {"number": 2, "state": "open"}, scheduler_checks._blockers),
    }[kind]
    payload = [{**saved, **deepcopy(FUTURE)}]
    store.refresh_local_issues([issue], [], {}, NOW)
    store.record_read(7, kind, payload, baseline=store.read_baseline())
    store.refresh_local_issues([issue], [], {}, NOW + timedelta(seconds=1))
    reopened = StateStore(store.root)
    assert reopened.confirmed_read(7, kind) == (payload,), "Fixture must exercise the persisted cache hit"
    ctx = SimpleNamespace(store=reopened, tracker=SimpleNamespace())
    result = read_record(kind, lambda: read(ctx, 7, confirmed=True))
    assert len(result) == 1, f"{kind} must not discard future cache records"
    assert_fields(result[0], cls, saved)


@pytest.mark.parametrize("reader", ["orchestrator", "scheduler"])
def test_cached_comments_tolerate_future_fields(store, reader):
    from foragent import orch, scheduler_checks
    from foragent.config import Config
    from foragent.tracker import Issue

    issue = Issue(7, "Task", "body", frozenset({"blocked"}), "open", updated_at=STAMP)
    comment = {"id": 1, "author": "foragent", "created_at": STAMP,
               "body": "<!-- foragent:blocked reason=spec-question -->\nChoose a locale.", **deepcopy(FUTURE)}
    store.refresh_local_issues([issue], [], {}, NOW, comments={7: [comment]})
    ctx = SimpleNamespace(store=StateStore(store.root), now=lambda: NOW, config=Config(), tracker=SimpleNamespace())
    if reader == "orchestrator":
        result = read_record("orch.list_blocked comments", lambda: orch.list_blocked(ctx))
        assert len(result) == 1 and result[0]["number"] == 7
        assert result[0]["reason"] == "spec-question" and result[0]["details"] == comment["body"]
    else:
        report = SimpleNamespace(actions=[])
        result = read_record("scheduler cached comments", lambda: scheduler_checks._escalate_overdue_blocker(ctx, issue, report))
        assert result.number == 7 and result.labels == issue.labels and report.actions == []


def test_stop_hook_reads_future_approval_fields_without_blocking_valid_checks(store, monkeypatch, capsys):
    from foragent import runner
    from foragent.run_checks import CheckResult, check_done

    path = write_json(store, "approvals/7.json", {**APPROVAL, **deepcopy(FUTURE)})
    calls = []

    def check(command, worktree, log, timeout_s):
        calls.append((command, worktree))
        return CheckResult(command, 0, False, "offline check passed")

    monkeypatch.setattr(runner, "_check", check)
    assert check_done(path, store.root, "tests/acceptance/") == 0, (
        "A future approval field must not block the stop hook: " + capsys.readouterr().err
    )
    assert calls == [("true", store.root)]


def test_order_progress_and_baseline_cache_keep_compatibility_with_dataclass_records(store):
    from foragent.run_baseline import AcceptanceBaseline, BaselineBuild, _saved_baseline

    progress = {"status": "preparing", "step": "read", **deepcopy(FUTURE)}
    write_json(store, "order-progress.json", progress)
    assert store.order_progress() == progress
    expected = {**progress, "step": "ready"}
    assert store.update_order_progress(lambda old: {**old, "step": "ready"}) == expected
    assert StateStore(store.root).order_progress() == expected

    cache = StateStore(store.root / "baselines", now=lambda: NOW)
    for coverage in ({}, {"collected_ids": ["test_ok", "test_fail"], "failing_ids": ["test_fail"], "excluded": []}):
        saved = {"created_at": STAMP, "node_ids": ["test_ok"], **coverage, **deepcopy(FUTURE)}
        write_json(cache, "baseline.json", saved)
        loaded = cache.load_acceptance_baseline("baseline")
        assert loaded == saved
        baseline = _saved_baseline("pytest", loaded)
        assert baseline == AcceptanceBaseline(
            "pytest", ("test_ok",),
            tuple(coverage["collected_ids"]) if coverage else None,
            tuple(coverage["failing_ids"]) if coverage else None,
        )
    # BaselineBuild is also serialized to JSON for the background reader.
    build = BaselineBuild("base", "pytest", [], "key", "scope")
    write_json(cache, "build.json", {**asdict(build), **deepcopy(FUTURE)})
    assert load_record()(BaselineBuild, read_json(cache.root / "build.json")) == build


def test_user_configuration_rejects_unknown_keys_while_state_records_ignore_them(store, tmp_path):
    from foragent.config import ConfigError, load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".foragent.yml").write_text("max_paralell: 2\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown configuration key: max_paralell"):
        load_config(repo)

    # The same unknown key is harmless in a machine-written state record.
    write_json(store, "inspect.json", {"max_paralell": 2})
    record = read_record("state record with an unknown configuration key", store.load_inspect)
    assert_fields(record, InspectState, {})


def test_ast_dataclass_mapping_load_sites_use_the_public_tolerant_loader(store):
    source = Path(__file__).resolve().parents[2] / "src/foragent"
    trees = {path: ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
             for path in sorted(source.rglob("*.py"))}
    assert trees, f"Cannot enumerate dataclass load sites under {source}"

    def leaf(node):
        if isinstance(node, ast.Call):
            return leaf(node.func)
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None

    dataclasses = set()
    for tree in trees.values():
        decorator_names = {"dataclass"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "dataclasses":
                decorator_names.update(alias.asname or alias.name for alias in node.names if alias.name == "dataclass")
        dataclasses.update(node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
                           and any(leaf(decorator) in decorator_names for decorator in node.decorator_list))
    assert {"Run", "Approval", "Flag", "InspectState"} <= dataclasses

    # Config is user-written configuration, not a persisted state record.
    # Its unknown or misspelled keys must continue to raise ConfigError.
    dataclasses.discard("Config")

    unsafe = []
    for path, tree in trees.items():
        constructor_names = set(dataclasses)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                constructor_names.update(alias.asname or alias.name for alias in node.names if alias.name in dataclasses)
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            # Mapping-only constructors hydrate records. Run(..., **session) creates
            # a new run from explicit arguments and is deliberately outside this guard.
            if not isinstance(node, ast.Call) or node.args or not any(key.arg is None for key in node.keywords):
                continue
            if leaf(node.func) not in constructor_names | {"cls"}:
                continue
            parent = parents.get(node)
            while parent is not None and not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                parent = parents.get(parent)
            if path == source / "records.py" and parent is not None and parent.name == "load_record":
                continue
            unsafe.append(f"{path.relative_to(source.parent.parent)}:{node.lineno}: {ast.unparse(node)}")
    assert not unsafe, (
        f"Dataclass mapping load sites must use {LOADER}(cls, data); direct constructors are version-fragile:\n"
        + "\n".join(sorted(unsafe))
    )
    load_record()
