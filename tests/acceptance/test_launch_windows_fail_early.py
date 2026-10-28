"""Launch A4: a cold console entry handles missing fcntl without a traceback."""

import importlib
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

from test_skills_autosync import cli_exit, isolated_home


def test_console_entry_without_fcntl_prints_one_unsupported_line(monkeypatch, capsys):
    root = Path(__file__).resolve().parents[2]
    target = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]["fa"]
    module_name, function = target.split(":")

    def forbidden(*args, **kwargs):
        pytest.fail("Unsupported-platform detection must not start a subprocess")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    # Conftest already imported Foragent. A cold import must exercise the actual
    # console target, not a cached cli.main hiding launchd/git's fcntl import.
    saved = {name: module for name, module in sys.modules.copy().items()
             if name == "foragent" or name.startswith("foragent.")}
    code, escaped = None, None
    try:
        for name in saved:
            sys.modules.pop(name, None)
        with monkeypatch.context() as patch:
            patch.setitem(sys.modules, "fcntl", None)
            try:
                entry = getattr(importlib.import_module(module_name), function)
                code = cli_exit(entry, ["init"])
            except SystemExit as exc:
                code = exc.code
            except ImportError as exc:
                escaped = exc
    finally:
        for name in list(sys.modules):
            if name == "foragent" or name.startswith("foragent."):
                sys.modules.pop(name, None)
        sys.modules.update(saved)
    captured = capsys.readouterr()
    assert escaped is None, f"Console import leaks {type(escaped).__name__}: {escaped}; print the unsupported line instead"
    assert isinstance(code, int) and code != 0
    lines = [line.strip() for line in (captured.out + captured.err).splitlines() if line.strip()]
    assert len(lines) == 1, f"Expected one unsupported-platform line, got {lines!r}"
    assert "windows" in lines[0].lower() and "not supported yet" in lines[0].lower()
    assert "Traceback" not in captured.out + captured.err
