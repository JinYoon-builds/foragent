"""Acceptance test for capacity-aimd. Locked by Foragent; do not edit."""

import pytest

from test_skills_autosync import isolated_home


@pytest.mark.parametrize("current,work,ceiling,expected", [(2, 8, None, 3), (2, 2, None, 2), (2, 8, 2, 2)])
def test_capacity_aimd_headroom_adds_one_with_work_and_ceiling_caps(current, work, ceiling, expected):
    from foragent.scheduler import next_parallel

    assert next_parallel(current, pressure="normal", load_per_cpu=0.5, lanes_with_work=work,
                         ceiling=ceiling, increase_ok=True) == expected


@pytest.mark.parametrize("pressure,current,expected", [("warn", 7, 3), ("critical", 8, 4), ("critical", 1, 1)])
def test_capacity_aimd_pressure_halves_immediately_without_increase_permission(pressure, current, expected):
    from foragent.scheduler import next_parallel

    assert next_parallel(current, pressure=pressure, load_per_cpu=0.5, lanes_with_work=10,
                         ceiling=None, increase_ok=False) == expected


@pytest.mark.parametrize("load,increase_ok,expected", [(1.5, True, 3), (2.0, False, 3), (0.5, False, 6)])
def test_capacity_aimd_high_load_halves_and_normal_load_waits(load, increase_ok, expected):
    from foragent.scheduler import next_parallel

    assert next_parallel(6, pressure="normal", load_per_cpu=load, lanes_with_work=10,
                         ceiling=None, increase_ok=increase_ok) == expected


def test_capacity_aimd_config_defaults_to_no_ceiling_but_accepts_explicit_cap(tmp_path):
    from foragent.config import Config, load_config

    assert Config().max_parallel is None
    assert load_config(tmp_path).max_parallel is None
    (tmp_path / ".foragent.yml").write_text("max_parallel: 3\n")
    config = load_config(tmp_path)
    assert config.max_parallel == 3
    from foragent.scheduler import next_parallel

    assert next_parallel(3, pressure="normal", load_per_cpu=0.5, lanes_with_work=8,
                         ceiling=config.max_parallel, increase_ok=True) == 3


def test_capacity_aimd_machine_load_smoke_returns_valid_pair():
    from foragent.scheduler import machine_load

    pressure, load_per_cpu = machine_load()
    assert pressure in {"normal", "warn", "critical"}
    assert isinstance(load_per_cpu, (int, float)) and load_per_cpu >= 0
