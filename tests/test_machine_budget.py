"""Unit tests for machine.yml loading (#365)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json

import pytest

from foragent import capacity, machine_budget
from foragent.config import Config

NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)


def write(text):
    path = machine_budget.machine_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def report():
    return machine_budget.read().report()


def test_defaults_come_from_the_config_class():
    config = Config()
    assert machine_budget.DEFAULTS == {
        "test_slots": config.test_slots, "test_slots_floor": config.test_slots_floor,
        "parallel_floor": config.parallel_floor, "max_parallel": config.max_parallel,
    }


def test_without_file_every_key_is_the_fixed_default():
    assert report() == {key: {"value": value, "source": "default"}
                        for key, value in machine_budget.DEFAULTS.items()}


def test_file_values_win_and_missing_keys_use_defaults():
    write("test_slots: 4\nparallel_floor: 2\n")
    assert machine_budget.values() == {
        "test_slots": 4, "test_slots_floor": 1, "parallel_floor": 2, "max_parallel": None,
    }
    assert report()["test_slots"] == {"value": 4, "source": "machine"}
    assert report()["test_slots_floor"]["source"] == "default"


@pytest.mark.parametrize("value", ["0", "-1", "null", "two", "1.5", "true", "false", "[1]"])
def test_invalid_value_marks_only_its_key_invalid(value):
    write(f"test_slots: {value}\nmax_parallel: 3\n")
    result = report()
    assert result["test_slots"] == {"value": 2, "source": "invalid"}
    assert result["max_parallel"] == {"value": 3, "source": "machine"}


# A top-level null (also an empty file) is not a mapping either.
@pytest.mark.parametrize("text", ["a: [\n", "- 1\n", "just text\n", "null\n", "~\n", ""])
def test_unreadable_file_marks_every_key_invalid(text):
    write(text)
    result = report()
    assert {item["source"] for item in result.values()} == {"invalid"}
    assert machine_budget.values() == machine_budget.DEFAULTS


def test_floor_above_its_ceiling_marks_only_the_floor_invalid():
    write("test_slots: 2\ntest_slots_floor: 3\nmax_parallel: 1\nparallel_floor: 2\n")
    result = report()
    assert result["test_slots"] == {"value": 2, "source": "machine"}
    assert result["test_slots_floor"] == {"value": 1, "source": "invalid"}
    assert result["max_parallel"] == {"value": 1, "source": "machine"}
    assert result["parallel_floor"] == {"value": 1, "source": "invalid"}


def test_unknown_keys_are_ignored_and_listed_once():
    write("test_slots: 3\nturbo: 9\nalpha: 1\n")
    result = report()
    assert result["test_slots"] == {"value": 3, "source": "machine"}
    assert result["unknown_keys"] == ["alpha", "turbo"]
    write("test_slots: 3\n")
    assert "unknown_keys" not in report()


def test_file_is_read_on_every_call_without_a_cache():
    write("test_slots: 3\n")
    assert machine_budget.values()["test_slots"] == 3
    write("test_slots: 1\n")
    assert machine_budget.values()["test_slots"] == 1


def test_effective_test_slots_never_exceeds_the_machine_ceiling():
    assert capacity.effective_test_slots(6) == 2  # No machine.yml: the fixed default.
    write("test_slots: 3\n")
    assert capacity.effective_test_slots(6) == 3
    assert capacity.effective_test_slots(1) == 1


def test_lower_ceiling_clamps_stored_test_slots_at_once_without_decreased_at():
    root = capacity.machine_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / capacity.TEST_SLOTS_FILE).write_text(json.dumps(
        {"slots": 5, "changed_at": (NOW - timedelta(seconds=10)).isoformat()}))
    # Normal pressure and an increase-ready interval would otherwise grow the value.
    value = capacity.update_test_slots(3, NOW, pressure="normal", load_per_cpu=0.1, swapins=0)
    assert value == 3
    state = capacity.read_test_slots()
    assert state["slots"] == 3 and "decreased_at" not in state
    rows = [json.loads(line) for line in (root / capacity.HISTORY_FILE).read_text().splitlines()]
    assert [(row["from"], row["to"]) for row in rows] == [(5, 3)]
    # AIMD resumes on the next tick: warn removes a slot without waiting for an interval.
    assert capacity.update_test_slots(3, NOW + timedelta(seconds=1), pressure="warn",
                                      load_per_cpu=0.1, swapins=4) == 2



@pytest.mark.parametrize("language", ["en", "ko"])
def test_status_prints_machine_values_unknown_keys_and_ignored_repo_floors(tmp_path, capsys, language):
    from foragent import cli

    write("test_slots: 3\nparallel_floor: 0\nturbo: 1\n")
    (tmp_path / ".foragent.yml").write_text("test_slots_floor: 2\n", encoding="utf-8")
    status = {"machine_budget": report(), "repo_floors_ignored": cli._repo_floor_keys(tmp_path)}
    cli._print_machine_budget(status, language)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3
    assert "test_slots 3" in lines[0] and "max_parallel" in lines[0]
    assert "turbo" in lines[1]
    assert "test_slots_floor" in lines[2]


def test_status_omits_floor_line_when_repository_sets_no_floor(tmp_path, capsys):
    from foragent import cli

    (tmp_path / ".foragent.yml").write_text("test_slots: 4\n", encoding="utf-8")
    assert cli._repo_floor_keys(tmp_path) == []
    cli._print_machine_budget({"machine_budget": report(), "repo_floors_ignored": []}, "en")
    assert capsys.readouterr().out.splitlines() == [
        "Machine budget (machine.yml): test_slots 2 (default), test_slots_floor 1 (default), "
        "parallel_floor 1 (default), max_parallel none (default)",
    ]
