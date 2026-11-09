"""Shared world for docs-signal-new-mechanism acceptance tests (helper, no tests).

Built on the existing role-signal worlds (test_role_signals.signal_case and
test_role_signal_raise.raise_at). The fa command tree is changed the way a merge
changes it: a new top-level group is a new module on the foragent.commands
package path (as test_cli_split does), and a new nested action is one more
add_parser call inside an existing group. A new shelf place is one more entry
in the shelf's place list (foragent.shelf.places()).
"""

import argparse
import ast
import dataclasses
import importlib
from pathlib import Path
import re
import sys

from test_role_signal_raise import raise_at
from test_role_signals import completed, signal_case
from test_role_source import write_role_snapshot

DOCS_SIGNALS = [{"paths": ["docs/**", "README*"], "kinds": []}]


def command_paths(parser=None):
    """Every subcommand path of the fa command tree, read from the real parser."""
    from foragent import cli

    parser = parser or cli._parser()
    found = []

    def walk(node, prefix):
        for action in node._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in dict.items(action.choices):
                    path = [*prefix, name]
                    found.append(" ".join(["fa", *path]))
                    walk(child, path)

    walk(parser, [])
    return found


def docs_world(tmp_path, monkeypatch):
    """A role-signal world whose docs role has the default docs signal and whose
    repository map already names every current place and subcommand."""
    from foragent import shelf

    case = signal_case(tmp_path, monkeypatch)
    case.roles["docs"]["signals"] = [dict(signal) for signal in DOCS_SIGNALS]
    write_role_snapshot(case.root, case.roles)
    # The map names everything that exists before the merge, so "new" can only
    # mean what the merge added, whatever baseline the implementation keeps.
    lines = ["# Mechanisms", ""]
    lines += [f"- shelf place `{place.name}`" for place in shelf.places()]
    lines += [f"- `{path}`" for path in command_paths()]
    (case.root / "docs").mkdir(exist_ok=True)
    (case.root / "docs/mechanisms.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    case.monkeypatch = monkeypatch
    return case


def docs_requests(case):
    from foragent.orch_inbox import requests

    return [row for row in requests(case.store) if row.get("role") == "docs"]


def tick(case, at):
    """One raise step of the gate tick at ``at``; returns the docs requests it added."""
    return [row for row in raise_at(case, at) if row.get("role") == "docs"]


def merge(case, number, at, paths):
    """A merged run finishing at ``at`` that changed ``paths``."""
    completed(case, number, finished=at, paths=list(paths))


def add_place(case, name):
    """Add one shelf place to the place list, as a merge of shelf.py would."""
    from foragent import shelf

    template = shelf.places()[0]
    place = dataclasses.replace(template, name=name, purpose=f"Keep {name} items",
                                trigger=f"{name} item recorded (fa orch {name} add)")
    case.monkeypatch.setattr(shelf, "_PLACES", (*shelf._PLACES, place))
    assert name in [item.name for item in shelf.places()], "world: the place list must show the new place"
    return place


def add_nested_command(case, group, action):
    """Add ``fa orch <action>`` to the command tree, as a merge of an orch module would."""
    from foragent import orch_roles

    assert group == "orch"
    original = orch_roles.add_parser

    def add_parser(orch_actions):
        original(orch_actions)
        orch_actions.add_parser(action, help=f"Probe action {action}")

    case.monkeypatch.setattr(orch_roles, "add_parser", add_parser)
    assert f"fa orch {action}" in command_paths(), "world: the command tree must show the new action"


def add_group_command(case, tmp_path, name):
    """Add top-level ``fa <name>`` as a new module of the commands package (test_cli_split's way)."""
    import foragent.commands as package

    source = Path(importlib.import_module("foragent.commands.bench").__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "bench":
            node.value = name
    directory = tmp_path / "merged-commands"
    directory.mkdir(exist_ok=True)
    (directory / f"{name}.py").write_text(ast.unparse(tree) + "\n", encoding="utf-8")
    before = set(sys.modules)
    case.monkeypatch.setattr(package, "__path__", [*package.__path__, str(directory)])
    importlib.invalidate_caches()

    def forget():
        for module in set(sys.modules) - before:
            if module.startswith(package.__name__ + "."):
                del sys.modules[module]

    case.forget = forget
    assert f"fa {name}" in command_paths(), "world: the command tree must show the new group"


def names(text, word):
    """Whether ``word`` appears in ``text`` as a whole token."""
    return re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", text) is not None
