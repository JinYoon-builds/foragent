"""Acceptance tests for n4-fseventsd-resource-facts: fseventsd memory and CPU in machine facts."""

import json

import pytest

from fseventsd_facts_support import (
    CPU_PERCENT, FSEVENTSD_PID, HUMAN_CPU, HUMAN_MEMORY, RSS_BYTES, SAMPLE_AGE_SECONDS, advance,
    case_with_table, cpu_with_unit, fseventsd_line, fseventsd_reading, gate_tick, leaves, memory_with_unit,
    numeric_zero_readings, run_facts, sample_age, unavailable_reading,
)


def test_macos_gate_sample_shows_fseventsd_memory_cpu_units_and_age(tmp_path, monkeypatch, capsys):
    case = case_with_table(tmp_path, monkeypatch)
    case.table.set(fseventsd=True)
    gate_tick(case, monkeypatch, macos=True)
    advance(case)

    code, out, err = run_facts(case, capsys)
    assert code == 0, err
    subtree, ancestors = fseventsd_reading(json.loads(out))
    assert memory_with_unit(subtree), f"fseventsd resident memory {RSS_BYTES} bytes (with unit) missing: {subtree}"
    assert cpu_with_unit(subtree), f"fseventsd CPU {CPU_PERCENT} percent (with unit) missing: {subtree}"
    assert sample_age(subtree, ancestors) == SAMPLE_AGE_SECONDS, "The reading's sample age is not stated"

    code, out, err = run_facts(case, capsys, human=True)
    assert code == 0, err
    line = fseventsd_line(out)
    assert HUMAN_MEMORY.search(line), f"Human facts lack fseventsd resident memory with its unit: {line}"
    assert HUMAN_CPU.search(line), f"Human facts lack fseventsd CPU percentage: {line}"
    assert f"{SAMPLE_AGE_SECONDS} s" in out or f"{SAMPLE_AGE_SECONDS}s" in out


def test_facts_report_the_gate_sample_not_a_live_process_read(tmp_path, monkeypatch, capsys):
    """The gate owns the sample: a facts run shows its values even after the process changed."""
    case = case_with_table(tmp_path, monkeypatch)
    case.table.set(fseventsd=True)
    gate_tick(case, monkeypatch, macos=True)
    case.table.set(fseventsd=True, rss=3 * RSS_BYTES, cpu=99.0)  # Live now; the sample says otherwise.
    advance(case)

    code, out, err = run_facts(case, capsys)
    assert code == 0, err
    subtree, _ = fseventsd_reading(json.loads(out))
    assert memory_with_unit(subtree) and cpu_with_unit(subtree), subtree
    assert 3 * RSS_BYTES not in [value for _, value in leaves(subtree)]


@pytest.mark.parametrize("situation", ["absent", "inaccessible", "unsupported"])
def test_unavailable_fseventsd_is_explicit_not_zero(tmp_path, monkeypatch, capsys, situation):
    case = case_with_table(tmp_path, monkeypatch)
    if situation == "absent":
        case.table.set(fseventsd=False)
    elif situation == "inaccessible":
        case.table.set(fseventsd=True, denied=True)
    else:
        case.table.set(fseventsd=False)
    gate_tick(case, monkeypatch, macos=situation != "unsupported")
    advance(case)

    code, out, err = run_facts(case, capsys)
    assert code == 0, err
    subtree, _ = fseventsd_reading(json.loads(out))
    assert unavailable_reading(subtree), f"No explicit unavailable fseventsd reading ({situation}): {subtree}"
    assert numeric_zero_readings(subtree) == [], f"Unavailable fseventsd reported as measured zero: {subtree}"

    code, out, err = run_facts(case, capsys, human=True)
    assert code == 0, err
    line = fseventsd_line(out).lower()
    assert any(word in line for word in ("unavailable", "unknown", "absent", "denied", "unsupported",
                                         "not running", "모름", "없음")), line
    assert not HUMAN_MEMORY.search(line)
    assert "0 b" not in line and "0 mib" not in line and "0%" not in line.replace(" ", "").replace("100%", "")


def test_older_sample_without_process_fields_stays_readable_and_unknown(tmp_path, monkeypatch, capsys):
    """A valid sample written before this issue keeps its machine totals; fseventsd is unknown."""
    from datetime import timedelta

    from foragent import capacity, resource_sample

    case = case_with_table(tmp_path, monkeypatch)
    case.table.set(fseventsd=True)  # Must not be read by facts: the stored sample predates the field.
    legacy = {"at": case.clock[0].isoformat(), "cpu_busy": 0.25, "load_average_per_cpu": 0.9,
              "memory_pressure": "warn", **dict.fromkeys(capacity.DISK_RATES.values(), 1.0), "test_slot_queue": 0}
    path = resource_sample.path_for(case.store)
    path.write_text(json.dumps(legacy), encoding="utf-8")
    case.clock[0] += timedelta(seconds=SAMPLE_AGE_SECONDS)

    code, out, err = run_facts(case, capsys)
    assert code == 0, err
    facts = json.loads(out)
    assert facts["resources"]["machine"]["memory_pressure"] == "warn", "The older sample is no longer readable"
    assert facts["resources"]["machine"]["age_seconds"] == SAMPLE_AGE_SECONDS
    subtree, _ = fseventsd_reading(facts)
    assert unavailable_reading(subtree), f"Missing process fields in an older sample must be unknown: {subtree}"
    assert numeric_zero_readings(subtree) == []
    assert not memory_with_unit(subtree), "Facts read the live process instead of the stored sample"


def test_facts_send_no_signal_and_leave_the_sample_unchanged(tmp_path, monkeypatch, capsys):
    from foragent import resource_sample

    case = case_with_table(tmp_path, monkeypatch)
    case.table.set(fseventsd=True)
    path = gate_tick(case, monkeypatch, macos=True)
    assert case.table.signals == [], f"The gate signalled a process while sampling: {case.table.signals}"
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    stored = resource_sample.read(path)
    advance(case)

    for human in (False, True, False):
        code, out, err = run_facts(case, capsys, human=human)
        assert code == 0, err
        assert "fseventsd" in out.lower(), "Facts do not show the sampled fseventsd reading"
    assert case.table.signals == [], f"fa orch facts signalled a process: {case.table.signals}"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before, "fa orch facts rewrote the gate's sample"
    assert resource_sample.read(path) == stored
    assert not any(pid == FSEVENTSD_PID for pid, _ in case.table.signals)
