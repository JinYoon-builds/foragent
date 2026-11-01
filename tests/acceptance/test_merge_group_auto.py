"""Acceptance contract for journal-derived automatic merge group limits."""

from datetime import timedelta
import json

import pytest
import yaml

from foragent import cli, orch_events
from foragent.config import ConfigError, load_config
from test_orch_review_handoff import bind_cli, make_case
from test_skills_autosync import cli_exit, isolated_home


HISTORY = (
    ("passed", 4, 5),
    ("passed", 5, 6),
    ("passed", 3, 6),
    ("failed", 6, 5),
    ("void", 3, 5),
)


@pytest.fixture
def merge_case(git_history, tmp_path, monkeypatch, isolated_home):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], language="en")
    case.main = bind_cli(case, monkeypatch)
    # Keep the existing temporary repo/store binding, but exercise real YAML loading.
    monkeypatch.setattr(cli, "load_config", load_config)
    configure(case)
    return case


def configure(case, **values):
    (case.root / ".foragent.yml").write_text(yaml.safe_dump({
        "repo": "owner/repo", "skill_libraries": [], "language": "en", **values,
    }), encoding="utf-8")


def record_check(case, verdict, count):
    case.clock[0] += timedelta(seconds=1)
    orch_events.append_event(
        case.store, case.clock[0], 1, "group-check", f"group check {verdict}",
        members=[{"n": n, "head_sha": f"{n:040x}"} for n in range(1, count + 1)],
        verdict=verdict, passed=verdict == "passed",
    )


def state_files(case):
    return {path.relative_to(case.store.root): path.read_bytes()
            for path in case.store.root.rglob("*") if path.is_file()}


def assert_group(case, capsys, limit, mode):
    config_path = case.root / ".foragent.yml"
    config_before, state_before = config_path.read_bytes(), state_files(case)
    # Repeated reads must replay the journal consistently without persisting a limit.
    for _ in range(2):
        code = cli_exit(case.main, ["status", "--json", "--repo-root", str(case.root)])
        output = capsys.readouterr()
        assert code == 0, f"fa status --json failed: {output.err}"
        payload = json.loads(output.out)
        assert payload.get("merge_group") == {"limit": limit, "mode": mode}
        assert type(payload["merge_group"]["limit"]) is int
        assert load_config(case.root).merge_group_max == ("auto" if mode == "auto" else limit)
        assert config_path.read_bytes() == config_before
        assert state_files(case) == state_before, "The limit must be derived, not stored"


def test_missing_key_and_empty_journal_default_to_auto_four(merge_case, capsys):
    assert "merge_group_max" not in yaml.safe_load(
        (merge_case.root / ".foragent.yml").read_text(encoding="utf-8"))
    assert not (merge_case.store.root / "orch-events.jsonl").exists()
    assert_group(merge_case, capsys, 4, "auto")


def test_explicit_auto_replays_each_verdict_oldest_first(merge_case, capsys):
    configure(merge_case, merge_group_max="auto")
    for verdict, count, expected in HISTORY:
        record_check(merge_case, verdict, count)
        # Checking every prefix catches errors hidden by the final halving.
        assert_group(merge_case, capsys, expected, "auto")


def test_default_auto_failures_stop_at_floor_two(merge_case, capsys):
    for count, expected in ((4, 3), (3, 2), (2, 2)):
        record_check(merge_case, "failed", count)
        assert_group(merge_case, capsys, expected, "auto")


def test_fixed_three_ignores_the_same_journal(merge_case, capsys):
    configure(merge_case, merge_group_max=3)
    assert_group(merge_case, capsys, 3, "fixed")
    for verdict, count, _ in HISTORY:
        record_check(merge_case, verdict, count)
        assert_group(merge_case, capsys, 3, "fixed")


def test_banana_uses_the_invalid_integer_config_error_path(merge_case, capsys):
    for invalid in (0, "banana"):
        configure(merge_case, merge_group_max=invalid)
        with pytest.raises(ConfigError, match="merge_group_max") as error:
            load_config(merge_case.root)
        code = cli_exit(merge_case.main, ["status", "--json", "--repo-root", str(merge_case.root)])
        output = capsys.readouterr()
        assert code == 1
        assert output.out == ""
        assert output.err.strip() == f"foragent: {error.value}"
