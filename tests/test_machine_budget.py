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
    assert machine_budget.DEFAULTS == {"test_slots": 2, "max_parallel": config.max_parallel}
    assert config.test_slots == 2


def test_without_file_every_key_is_the_fixed_default():
    assert report() == {key: {"value": value, "source": "default"}
                        for key, value in machine_budget.DEFAULTS.items()}


def test_file_values_win_and_missing_keys_use_defaults():
    write("test_slots: 4\n")
    assert machine_budget.values() == {"test_slots": 4, "max_parallel": None}
    assert report()["test_slots"] == {"value": 4, "source": "machine"}
    assert report()["max_parallel"]["source"] == "default"


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


@pytest.mark.parametrize("value", ["3", "0", "null", "two"])
def test_retired_floor_keys_are_accepted_and_ignored(value):
    # #497: floors are neither applied, reported nor listed as unknown, whatever their value.
    write(f"test_slots: 2\ntest_slots_floor: {value}\nmax_parallel: 1\nparallel_floor: {value}\n")
    assert report() == {"test_slots": {"value": 2, "source": "machine"},
                        "max_parallel": {"value": 1, "source": "machine"}}


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
    assert capacity.effective_test_slots() == 2  # No machine.yml: the fixed default.
    write("test_slots: 3\n")
    assert capacity.effective_test_slots() == 3
    write("test_slots: 1\n")
    assert capacity.effective_test_slots() == 1


@pytest.mark.parametrize("configured", [None, 1, 4])
def test_file_slot_origin_survives_config_copies_but_explicit_overrides_work(tmp_path, configured):
    from copy import deepcopy
    from dataclasses import asdict
    import yaml

    from foragent.config import load_config

    write("test_slots: 12\n")
    settings = {} if configured is None else {"test_slots": configured}
    (tmp_path / ".foragent.yml").write_text(yaml.safe_dump(settings))
    config = load_config(tmp_path)
    for copied in (config, deepcopy(config), replace(config, language="ko"), Config(**asdict(config))):
        assert capacity.effective_test_slots(copied.test_slots) == 12
        assert yaml.safe_load(yaml.safe_dump(asdict(copied)))["test_slots"] == config.test_slots
    # Even an explicit override equal to the file's number has programmatic origin.
    overridden = replace(config, test_slots=int(config.test_slots))
    assert capacity.effective_test_slots(overridden.test_slots) == config.test_slots
    assert capacity.effective_test_slots(Config(test_slots=30).test_slots) == 12


def test_legacy_positional_slot_ceiling_controls_real_lock_admission(monkeypatch):
    import time

    write("test_slots: 3\n")
    monkeypatch.delenv(capacity.TEST_SLOT_ENV, raising=False)
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")
    with capacity.hold_test_slot(1):
        with pytest.raises(TimeoutError):
            with capacity.hold_test_slot(1, deadline=time.monotonic()):
                pytest.fail("A legacy ceiling must still serialize test commands")
        with capacity.hold_test_slot(3, deadline=time.monotonic()):
            pass
    with capacity.hold_test_slot(1, deadline=time.monotonic()):
        pass


@pytest.mark.parametrize("stored,expected", [(None, 12), (6, 6), (20, 12)])
def test_legacy_repo_test_slots_cannot_lower_machine_admission(tmp_path, monkeypatch, stored, expected):
    from unittest.mock import MagicMock

    from foragent.config import load_config

    write("test_slots: 12\n")
    (tmp_path / ".foragent.yml").write_text("test_slots: 4\n")
    with pytest.warns(UserWarning, match="test_slots is ignored"):
        config = load_config(tmp_path)
    assert config.test_slots == 4
    if stored is not None:
        (capacity.machine_root() / capacity.TEST_SLOTS_FILE).write_text(json.dumps(
            {"slots": stored, "changed_at": NOW.isoformat()}))
    admitted = []
    stream = MagicMock()
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")
    monkeypatch.setattr(capacity, "_take_test_slot", lambda limit, **kwargs: admitted.append(limit) or stream)
    monkeypatch.setattr(capacity.fcntl, "flock", lambda *args: None)
    with capacity.hold_test_slot(config.test_slots):
        assert admitted == [expected]
    stream.close.assert_called_once()


def test_waiting_test_slot_reads_changed_machine_capacity(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    write("test_slots: 8\n")
    limits, stream = [], MagicMock()

    def take(limit, **kwargs):
        limits.append(limit)
        return None if len(limits) == 1 else stream

    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")
    monkeypatch.setattr(capacity, "_take_test_slot", take)
    monkeypatch.setattr(capacity.fcntl, "flock", lambda *args: None)
    monkeypatch.setattr(capacity, "time", SimpleNamespace(sleep=lambda seconds: write("test_slots: 12\n")))
    with capacity.hold_test_slot():
        assert limits == [8, 12]
    stream.close.assert_called_once()


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
    # AIMD resumes on the next tick: overload removes a slot without waiting for an interval.
    assert capacity.update_test_slots(3, NOW + timedelta(seconds=1), pressure="normal",
                                      load_per_cpu=2.0, swapins=4) == 2


@pytest.mark.parametrize("language", ["en", "ko"])
def test_status_prints_machine_values_and_unknown_keys(capsys, language):
    from foragent import cli

    write("test_slots: 3\nparallel_floor: 0\nturbo: 1\n")
    cli._print_machine_budget({"machine_budget": report()}, language)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert "test_slots 3" in lines[0] and "max_parallel" in lines[0] and "floor" not in lines[0]
    assert "turbo" in lines[1] and "floor" not in lines[1]


def test_status_shows_only_the_ceilings(capsys):
    from foragent import cli

    cli._print_machine_budget({"machine_budget": report()}, "en")
    assert capsys.readouterr().out.splitlines() == [
        "Machine budget (machine.yml): test_slots 2 (default), max_parallel none (default)",
    ]
