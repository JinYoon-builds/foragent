import os
from pathlib import Path
import sys

import foragent


def _run_init(monkeypatch, path):
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    source = Path(foragent.__file__).read_text()
    exec(compile(source, str(path), "exec"), {"__file__": str(path), "__name__": "foragent_copy"})
    return sys.dont_write_bytecode


def test_installed_package_never_writes_bytecode_whatever_its_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    installed = tmp_path / "tools/foragent/lib/python3.12/site-packages/foragent/__init__.py"
    assert _run_init(monkeypatch, installed) is True


def test_source_checkout_keeps_default_bytecode_behaviour(monkeypatch, tmp_path):
    checkout = tmp_path / "repo" / "src" / "foragent" / "__init__.py"
    assert _run_init(monkeypatch, checkout) is False
    lookalike = tmp_path / f"not-site-packages{os.sep}foragent" / "__init__.py"
    assert _run_init(monkeypatch, lookalike) is False
