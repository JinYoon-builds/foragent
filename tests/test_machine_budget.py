"""Unit tests for machine.yml loading (#365)."""

from dataclasses import replace

import pytest

from foragent import capacity, machine_budget
from foragent.config import Config, ConfigError


def write(text):
    path = machine_budget.machine_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_without_file_repository_values_apply_as_defaults():
    config = replace(Config(), test_slots=5, test_slots_floor=2, parallel_floor=3, max_parallel=7)
    assert machine_budget.configured() is None
    assert machine_budget.sources(config) == {
        "test_slots": {"value": 5, "source": "default"},
        "test_slots_floor": {"value": 2, "source": "default"},
        "parallel_floor": {"value": 3, "source": "default"},
        # A repository's max_parallel only limits its own runs; it never caps the machine.
        "max_parallel": {"value": None, "source": "default"},
    }


def test_file_values_win_and_missing_keys_use_built_in_defaults():
    write("test_slots: 4\nparallel_floor: 2\n")
    config = replace(Config(), test_slots=9, test_slots_floor=3, parallel_floor=1)
    assert machine_budget.values(config) == {
        "test_slots": 4, "test_slots_floor": 1, "parallel_floor": 2, "max_parallel": None,
    }
    assert machine_budget.configured() == machine_budget.values()


def test_empty_file_is_all_built_in_defaults():
    write("")
    assert machine_budget.configured() == machine_budget.DEFAULTS


@pytest.mark.parametrize("text", [
    "- 1\n", "test_slots: 0\n", "test_slots: two\n", "test_slots: true\n", "other: 1\n",
    "test_slots: 2\ntest_slots_floor: 3\n", "max_parallel: 1\nparallel_floor: 2\n", "a: [\n",
])
def test_invalid_file_is_a_config_error_naming_the_file(text):
    write(text)
    with pytest.raises(ConfigError, match="machine.yml"):
        machine_budget.values()


def test_effective_test_slots_lowers_but_never_raises_the_machine_ceiling():
    assert capacity.effective_test_slots(6) == 6  # No machine.yml: today's behaviour.
    write("test_slots: 3\n")
    assert capacity.effective_test_slots(6) == 3
    assert capacity.effective_test_slots(1) == 1
