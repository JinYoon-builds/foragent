"""Shared offline helpers for unit tests."""

import subprocess

import pytest


def record_gh(monkeypatch, reply=None):
    """Guard the shared CLI boundary, including tracker, init and board calls."""
    calls = []
    original = subprocess.run

    def run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv and str(argv[0]) == "gh":
            calls.append(list(argv))
            if reply is None:
                pytest.fail(f"Local observation called GitHub: {argv}")
            return subprocess.CompletedProcess(argv, 0, stdout=reply(argv[1:]), stderr="")
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return calls
