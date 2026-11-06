"""Shared offline helpers for unit tests."""

import pytest

from foragent import capacity, orch_architect


@pytest.fixture(autouse=True)
def fake_workloads(fake_process_table, monkeypatch):
    """Every stored workload PID belongs only to the fake table; never signal it."""
    monkeypatch.setattr(orch_architect, "os", capacity.os)
    yield fake_process_table
    assert fake_process_table.signals == [], "Capacity accounting must not signal any process"
