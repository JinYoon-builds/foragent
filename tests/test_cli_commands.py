"""Command groups read every former foragent.cli global through the facade."""

import ast
from pathlib import Path
from types import SimpleNamespace

from foragent import cli


def test_statusline_uninstall_uses_patched_facade_helper(monkeypatch):
    monkeypatch.setattr(cli, "_uninstall_statusline", lambda: 7)
    assert cli.main(["statusline", "--uninstall"]) == 7


def test_notify_uses_patched_facade_replace(tmp_path, monkeypatch):
    calls = []
    config = SimpleNamespace(repo="owner/repo")
    monkeypatch.setattr(cli, "load_config", lambda root: config)
    monkeypatch.setattr(cli, "replace", lambda value, **changes: calls.append(changes) or value)
    monkeypatch.setattr(cli.notify, "send", lambda *args: calls.append(args[1:]))
    assert cli.main(["notify", "--title", "t", "--line", "l", "--repo-root", str(tmp_path)]) == 0
    assert calls == [{"repo": "owner/repo"}, ("t", "l", None)]


def test_command_modules_import_nothing_that_cli_already_binds():
    package = Path(cli.__file__).parent / "commands"
    for path in package.glob("*.py"):
        if path.name == "__init__.py":
            continue
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = {alias.asname or alias.name for alias in node.names}
                assert names == {"facade"} or names <= {"facade", "repo_store"}, (path.name, names)
