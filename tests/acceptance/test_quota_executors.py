"""Acceptance test for quota-executors. Locked by Foragent; do not edit."""

from datetime import UTC, datetime, timedelta
from importlib import import_module, util
from pathlib import Path
from types import SimpleNamespace
import socket
import subprocess

import pytest
import yaml

from foragent.config import ConfigError, load_config
from foragent.orch_inbox import questions
from foragent.state import StateStore


NOW = datetime(2026, 10, 27, 23, 59, 59, tzinfo=UTC)


@pytest.fixture(autouse=True)
def offline_quota(monkeypatch):
    from foragent import executors, notify

    def forbidden(*args, **kwargs):
        raise AssertionError("Quota acceptance tests must use fakes, never external processes or network")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(executors.ClaudeExecutor, "run", forbidden)
    monkeypatch.setattr(executors.CodexExecutor, "run", forbidden)
    monkeypatch.setattr("builtins.input", forbidden)
    monkeypatch.setattr(notify, "send", lambda *args, **kwargs: True)


def _load(root, entries=None, **values):
    root.mkdir(parents=True, exist_ok=True)
    data = {"reviewer": "none", "checks": "local", "notify": "none", "skill_libraries": [], **values}
    if entries is not None:
        data["executors"] = entries
    (root / ".foragent.yml").write_text(yaml.safe_dump(data, sort_keys=False))
    error, config = None, None
    try:
        config = load_config(root)
    except ConfigError as exc:
        error = str(exc)
    assert error is None, f"Public config must accept executor quota settings: {error}"
    return config


def _entry(name, quota="subscription", **options):
    return {"name": name, "quota": quota, **options}


def _store(root, clock=None):
    return StateStore(root, now=lambda: NOW if clock is None else clock[0])


def _usage(store, name, percent, *, secondary=0, reset=None):
    reset = reset or store._now() + timedelta(days=7)
    # The existing public usage writer stores seen_at and these exact window fields.
    names = ("five_hour", "seven_day") if name == "claude" else ("5h", "7d")
    store.record_usage(name, [
        {"name": window, "used_percent": used, "resets_at": reset.isoformat()}
        for window, used in zip(names, (percent, secondary))
    ])


def _decide(store, config, *, number=1):
    # #107's public start contract, read from its unmerged scheduler_quota.py.
    name = "foragent.scheduler_quota"
    assert util.find_spec(name) is not None, "Public scheduler_quota.decide is required for executor admission"
    decide = getattr(import_module(name), "decide", None)
    assert callable(decide), "Public scheduler_quota.decide is required for executor admission"
    return decide(store, config, "main", store._now(), number)


def _choice(store, config, expected):
    start = _decide(store, config)
    if expected is None:
        assert start.held, "All configured executors are unavailable; the scheduler must wait"
    else:
        assert not start.held, f"Available configured executor {expected} must be admitted"
        assert start.config.executor == expected, "Fallback must follow the configured executor order"
    return start


def _spend(store, executor, amount):
    # New money records need a public writer; do not invent or patch an on-disk ledger.
    writer = getattr(store, "record_spend", None)
    assert callable(writer), "StateStore.record_spend(executor, amount) must record dated account-currency charges"
    writer(executor, amount)


def _offline_init(root, store, monkeypatch):
    from foragent import cli
    from foragent.init import init_repo
    from foragent.tracker import FakeTracker

    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".foragent.yml").exists():
        (root / ".foragent.yml").write_text("checks: local\nnotify: none\nskill_libraries: []\n")
    commits = []
    git = SimpleNamespace(
        check_worktree=lambda: None, worktree_root=lambda: root.resolve(),
        remote_slug=lambda: "owner/repo", uncommitted_paths=lambda paths: [],
        current_branch=lambda: "main", commit_paths=lambda *args: commits.append(args),
    )
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, *args, **kwargs: store))

    def gh_run(args, input=None):
        assert args == ["auth", "status"], "Init must use only the injected GitHub fake"
        return ""

    # Answer unrelated existing setup choices so only new quota questions are observable.
    global_path = Path.home() / ".config/foragent/config.yml"
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text("upstream_reports: false\n")
    monkeypatch.setattr(cli, "init_repo", lambda selected_root, **kwargs: init_repo(
        selected_root, git=git, gh_run=gh_run, tracker=FakeTracker(), **kwargs,
    ))
    monkeypatch.setattr(cli, "_open_lane_view", lambda root: None)
    assert cli.main(["init", "--repo-root", str(root), "--no-push", "--no-launchd", "--no-board",
                     "--statusline", "no"]) == 0, "Offline fa init must succeed without quota questions"
    assert questions(store, include_closed=True) == [], "Init must not ask about future quota or credits"


def test_ordered_executor_lists_have_no_two_executor_ceiling(tmp_path):
    names = ["claude", "codex", "local", "batch", "spare", "last"]
    for count in (1, 2, 3, 6):
        entries = [_entry(name, "unlimited") for name in names[:count]]
        config = _load(tmp_path / str(count), entries)
        assert [entry["name"] for entry in config.executors] == names[:count], "Config must retain list order"
        assert all(entry.get("keep_percent") is None for entry in config.executors), "Keep mode is off when absent"
        _choice(_store(tmp_path / f"state-{count}"), config, names[0])


def test_unknown_quota_kinds_are_config_errors_not_unknown_top_level_keys(tmp_path):
    _load(tmp_path, [_entry("claude")])  # An unsupported executors key is not successful validation.
    for kind in ("credits", "unknown", "Subscription"):
        (tmp_path / ".foragent.yml").write_text(yaml.safe_dump({"executors": [_entry("claude", kind)]}))
        with pytest.raises(ConfigError, match="quota"):
            load_config(tmp_path)


def test_api_spend_requires_an_explicit_numeric_cap(tmp_path):
    _load(tmp_path, [_entry("codex", "api", spend_cap=10.5)])
    for options in ({}, {"spend_cap": "10"}, {"spend_cap": True}):
        (tmp_path / ".foragent.yml").write_text(yaml.safe_dump({"executors": [_entry("codex", "api", **options)]}))
        with pytest.raises(ConfigError, match="spend_cap"):
            load_config(tmp_path)


def test_claude_only_waits_at_its_limit_without_inventing_codex(tmp_path):
    clock = [NOW]
    config = _load(tmp_path / "repo", [_entry("claude")])
    store = _store(tmp_path / "state", clock)
    _usage(store, "claude", 99)
    _choice(store, config, "claude")
    until = NOW + timedelta(hours=5)
    _usage(store, "claude", 100, reset=until)
    store.pause_executor("claude", until)
    _choice(store, config, None)
    clock[0] = until
    _choice(store, config, "claude")


def test_codex_only_is_unaffected_by_claude_share_and_pause(tmp_path):
    config = _load(tmp_path / "repo", [_entry("codex")])
    store = _store(tmp_path / "state")
    _usage(store, "claude", 100, secondary=100)
    store.pause_executor("claude", NOW + timedelta(days=7))
    _usage(store, "codex", 99)
    _choice(store, config, "codex")
    store.pause_executor("codex", NOW + timedelta(hours=5))
    _choice(store, config, None)


def test_keep_percent_switches_at_the_boundary_in_either_list_order(tmp_path):
    for first, second in (("claude", "codex"), ("codex", "claude"), ("local", "codex")):
        config = _load(tmp_path / first, [_entry(first, keep_percent=80), _entry(second)])
        store = _store(tmp_path / f"state-{first}")
        _usage(store, first, 79)
        _usage(store, second, 2)
        _choice(store, config, first)
        _usage(store, first, 80)
        _choice(store, config, second)
        _usage(store, first, 6, secondary=81)
        _choice(store, config, second)
        assert questions(store) == [], "A keep threshold is not a hard limit and must not ask about credits"


def test_fallback_can_reach_a_third_executor_and_waits_when_all_are_paused(tmp_path):
    entries = [_entry("codex"), _entry("claude"), _entry("local", "unlimited")]
    config = _load(tmp_path / "repo", entries)
    store = _store(tmp_path / "state")
    for name in ("codex", "claude"):
        _usage(store, name, 100)
        store.pause_executor(name, NOW + timedelta(hours=5))
    _usage(store, "local", 100)  # Unlimited executors do not inherit subscription thresholds.
    _choice(store, config, "local")
    store.pause_executor("local", NOW + timedelta(hours=1))
    _choice(store, config, None)


def test_absent_keep_percent_and_expired_windows_do_not_force_fallback(tmp_path):
    store = _store(tmp_path / "state")
    config = _load(tmp_path / "repo", [_entry("claude"), _entry("codex")])
    _usage(store, "claude", 99)
    _choice(store, config, "claude")
    config = _load(tmp_path / "repo", [_entry("claude", keep_percent=80), _entry("codex")])
    _usage(store, "claude", 100, reset=NOW - timedelta(seconds=1))
    _choice(store, config, "claude")


def test_subscription_credits_are_opted_in_per_executor(tmp_path):
    store = _store(tmp_path / "state")
    config = _load(tmp_path / "repo", [_entry("claude"), _entry("codex", spend_cap=20)])
    _usage(store, "claude", 100)
    store.pause_executor("claude", NOW + timedelta(days=7))
    _choice(store, config, "codex")
    before = questions(store, include_closed=True)
    config = _load(tmp_path / "repo", [_entry("claude", spend_cap=10), _entry("codex")])
    _spend(store, "claude", 1)
    _choice(store, config, "claude")
    assert questions(store, include_closed=True) == before, "An explicitly capped executor must not ask again"


def test_daily_spend_cap_sums_charges_is_per_executor_and_resets_next_day(tmp_path):
    for quota in ("api", "subscription"):
        clock = [NOW]
        store = _store(tmp_path / f"state-{quota}", clock)
        config = _load(tmp_path / quota, [_entry("claude", quota, spend_cap=10), _entry("codex")])
        if quota == "subscription":
            _usage(store, "claude", 100)
            store.pause_executor("claude", NOW + timedelta(days=7))
        _spend(store, "codex", 100)
        _spend(store, "claude", 4)
        _spend(store, "claude", 5.5)
        _choice(store, config, "claude")
        _spend(store, "claude", 0.5)
        # A new process must see today's sum, not an attribute on the old store.
        store = _store(store.root, clock)
        _choice(store, config, "codex")
        single = _load(tmp_path / f"single-{quota}", [_entry("claude", quota, spend_cap=10)])
        _choice(store, single, None)
        clock[0] += timedelta(seconds=1)
        _choice(_store(store.root, clock), single, "claude")
        assert questions(store, include_closed=True) == [], "An explicit cap must not ask for spending permission"


def test_legacy_claude_quota_remains_the_two_subscription_shortcut(tmp_path):
    legacy = _load(tmp_path / "legacy", executor="claude", claude_quota={"mode": "keep", "keep_percent": 75})
    explicit = _load(tmp_path / "explicit", [_entry("claude", keep_percent=75), _entry("codex")])
    store = _store(tmp_path / "state")
    for percent, expected in ((74, "claude"), (75, "codex"), (100, "codex")):
        _usage(store, "claude", percent)
        _usage(store, "codex", 2)
        _choice(store, legacy, expected)
        _choice(store, explicit, expected)


def test_init_is_silent_and_first_hard_limit_records_one_durable_credits_question(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    store = _store(tmp_path / "state")
    _offline_init(root, store, monkeypatch)
    config = _load(root, [_entry("claude", keep_percent=80)])
    _offline_init(root, store, monkeypatch)
    _usage(store, "claude", 79)
    _choice(store, config, "claude")
    assert questions(store) == [], "Do not ask about a limit before it is reached"
    _usage(store, "claude", 100)
    store.pause_executor("claude", NOW + timedelta(hours=5))
    _choice(store, config, None)
    opened = questions(store)
    assert len(opened) == 1, "The first hard subscription limit must record one open human question"
    question = opened[0]
    assert "claude" in question["question"].lower(), "The question must identify the executor needing credits"
    assert question["status"] == "open" and question["answer"] is None
    assert [option.lower() for option in question["options"]] == [
        "wait for reset (recommended)", "allow credits up to a cap",
    ], "Offer a safe recommended wait and capped credits, never uncapped spending"
    assert question["recommend"] == 1, "Waiting for reset must be the recommended choice"
    for number in (1, 2, 3):
        _decide(_store(store.root), config, number=number)
    assert [q["id"] for q in questions(_store(store.root))] == [question["id"]], "Restarts must not duplicate the question"
