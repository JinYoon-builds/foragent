"""Acceptance test for facts-recorded-sample. Locked by Foragent; do not edit."""

from foragent import capacity
from test_facts_baseline_trend import (
    baseline_facts, isolated_home, offline_acceptance, prebuild,  # noqa: F401
    test_facts_baseline_trend_repeated_json_and_human_observation_is_read_only as _read_only_case,
)


def test_facts_recorded_sample_repeats_original_read_only_case_ten_times(baseline_facts, monkeypatch):
    """Exercise the original locked case, including its file/mtime and process guards."""
    case = baseline_facts
    live_calls = []

    def changing_live_load():
        load = 1.0 + len(live_calls) / 10
        live_calls.append(load)
        return "normal", load

    monkeypatch.setattr(capacity, "machine_load", changing_live_load)
    read = case.read
    observations = {"json": [], "human": []}

    def observed_read(*, human=False):
        output = read(human=human)
        previous = observations["human" if human else "json"]
        assert not previous or output == previous[0], (
            "Recorded facts must have identical JSON and human output across all ten runs")
        previous.append(output)
        return output

    monkeypatch.setattr(case, "read", observed_read)
    for _ in range(10):
        _read_only_case(case)

    assert len(observations["json"]) == 30
    assert len(observations["human"]) == 20
    assert live_calls == [], "The shared facts fixture must supply a recorded gate resource sample"
    machine = observations["json"][0]["resources"]["machine"]
    assert machine["at"] is not None, "Stable output must come from a recorded sample"
    assert machine["load_per_cpu"] == machine["load_average_per_cpu"]


def test_facts_recorded_sample_preserves_live_fallback_without_gate_records(
    baseline_facts, tmp_path, monkeypatch,
):
    """The real facts CLI still observes changing live inputs when no gate sample exists."""
    case = baseline_facts
    unrecorded_machine = tmp_path / "unrecorded-machine"
    monkeypatch.setattr(capacity, "machine_root", lambda: unrecorded_machine)
    live_calls = []
    loads = iter((1.03, 1.00, 0.25, 2.50))

    def changing_live_load():
        load = next(loads)
        live_calls.append(load)
        return "normal", load

    monkeypatch.setattr(capacity, "machine_load", changing_live_load)
    first = case.read()["resources"]["machine"]
    second = case.read()["resources"]["machine"]
    assert first["load_per_cpu"] == 1.03
    assert second["load_per_cpu"] == 1.00
    assert first["at"] is second["at"] is None
    assert "load 0.25 per CPU" in case.read(human=True)
    assert "load 2.50 per CPU" in case.read(human=True)
    assert live_calls == [1.03, 1.00, 0.25, 2.50], "Each observation must use the live reader"
    assert not unrecorded_machine.exists(), "Live observation must remain read-only"
