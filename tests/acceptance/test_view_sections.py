"""Acceptance test for view-sections. Locked by Foragent; do not edit."""

import ast
from contextlib import contextmanager
from dataclasses import replace
import importlib
import importlib.util
import inspect
import os
from pathlib import Path
import pkgutil
import signal
import sys

import pytest

from test_skills_autosync import isolated_home
from test_squirrel_running import plain_frame, running_snapshot, view_offline
from test_view_done_state import done_snapshot


# Main's visible groups become these modules; their internal helpers stay private.
EXISTING_SECTIONS = frozenset({"summary", "stage", "lanes", "activity", "detail", "help"})
SECTION_KEYS = frozenset({
    "status_unknown", "status_as_of", "watch_request_bubble", "watch_orchestrator",
    "watch_order", "watch_no_order", "watch_waiting", "watch_eta", "watch_complete",
    "watch_requests", "watch_agent", "watch_idle_lanes", "watch_done", "watch_now",
    "watch_lane", "watch_phase", "watch_retry", "watch_events", "watch_no_events",
    "watch_detail", "watch_paths", "watch_executor", "watch_model", "watch_skills",
    "watch_done_count", "watch_memory", "watch_help", "watch_help_many",
})


@pytest.fixture(autouse=True)
def sections_offline(view_offline, monkeypatch):
    """All frames use supplied snapshots; prohibit processes and signal delivery."""
    import subprocess

    def forbidden(*args, **kwargs):
        pytest.fail("view-sections acceptance must not run processes or send signals")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr(signal, "raise_signal", forbidden)


@contextmanager
def extra_sections(tmp_path, monkeypatch):
    """Public protocol for foragent.watch_sections.<feature>:

    ORDER is a unique integer; render(snapshot, config, repo_root, **context)
    returns list[watch_cells.Line] (styled spans). [] contributes no separator.
    Context carries tick, width, selected, now and layout from the existing frame
    API. Discover package.__path__ on each frame and compose in ORDER. Duplicate
    orders raise ValueError naming the order and both qualified module names.
    Only tmp_path receives source files; paths and imports are restored afterward.
    """
    assert importlib.util.find_spec("foragent.watch_sections") is not None, (
        "missing discoverable package foragent.watch_sections"
    )
    package = importlib.import_module("foragent.watch_sections")
    directory = tmp_path / "view-features"
    directory.mkdir()
    before = set(sys.modules)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(package, "__path__", [*package.__path__, str(directory)])
            importlib.invalidate_caches()
            yield package, directory
    finally:
        for name in set(sys.modules) - before:
            if name.startswith(package.__name__ + "."):
                module = sys.modules.pop(name)
                child = name.rsplit(".", 1)[-1]
                if getattr(package, child, None) is module:
                    delattr(package, child)
        importlib.invalidate_caches()


def section_modules(package):
    return [
        importlib.import_module(info.name)
        for info in pkgutil.iter_modules(package.__path__, package.__name__ + ".")
        if not info.ispkg
    ]


def write_section(directory, name, order, *, labels=None):
    expression = "[]" if labels is None else (
        f"[[({labels!r}[config.language] + f' #{{snapshot.order.number}}', '')]]"
    )
    (directory / f"{name}.py").write_text(
        f"ORDER = {order}\n\n"
        "def render(snapshot, config, repo_root, **context):\n"
        f"    return {expression}\n", encoding="utf-8",
    )
    importlib.invalidate_caches()


@pytest.mark.parametrize("language", ["en", "ko"])
def test_view_sections_discovers_appended_modules_at_their_order_positions(
    tmp_path, monkeypatch, language,
):
    from foragent import watch_layout

    snapshot, config = running_snapshot()
    config = replace(config, language=language)
    with extra_sections(tmp_path, monkeypatch) as (package, directory):
        orders = [module.ORDER for module in section_modules(package)]
        assert orders, "the existing frame must have discoverable sections"
        # Warm discovery before adding files. Names deliberately oppose ORDER.
        baseline = plain_frame(watch_layout.render_frame(snapshot, config, tmp_path))
        first = {"en": "FIRST SECTION", "ko": "첫 번째 구역"}
        last = {"en": "LAST SECTION", "ko": "마지막 구역"}
        write_section(directory, "acceptance_z_first", min(orders) - 10, labels=first)
        frame = plain_frame(watch_layout.render_frame(snapshot, config, tmp_path))
        assert frame.splitlines()[0] == first[language] + " #99"
        assert all(line in frame.splitlines() for line in baseline.splitlines() if line)
        # A second addition must be discovered after the first cached frame too.
        write_section(directory, "acceptance_a_last", max(orders) + 10, labels=last)
        lines = plain_frame(watch_layout.render_frame(snapshot, config, tmp_path)).splitlines()
        assert lines[0] == first[language] + " #99"
        assert lines[-1] == last[language] + " #99"
        assert lines.count(first[language] + " #99") == lines.count(last[language] + " #99") == 1


def test_view_sections_duplicate_order_names_both_modules(tmp_path, monkeypatch):
    from foragent import watch_layout

    snapshot, config = running_snapshot()
    with extra_sections(tmp_path, monkeypatch) as (package, directory):
        order = max(module.ORDER for module in section_modules(package)) + 10
        names = ("acceptance_duplicate_a", "acceptance_duplicate_b")
        for name in names:
            write_section(directory, name, order)
        with pytest.raises(ValueError) as error:
            watch_layout.render_frame(snapshot, config, tmp_path)
        message = str(error.value)
        assert str(order) in message, message
        assert all(f"{package.__name__}.{name}" in message for name in names), message


@pytest.mark.parametrize("language", ["en", "ko"])
@pytest.mark.parametrize("snapshot_factory", [running_snapshot, done_snapshot], ids=["running", "done"])
def test_view_sections_empty_section_adds_no_blank_line(
    tmp_path, monkeypatch, language, snapshot_factory,
):
    from foragent import watch_layout

    snapshot, config = snapshot_factory()
    config = replace(config, language=language)
    with extra_sections(tmp_path, monkeypatch) as (package, directory):
        modules = sorted(section_modules(package), key=lambda module: module.ORDER)
        assert len(modules) >= 2, "the existing frame must have multiple sections"
        # Reserve an interior gap without changing any existing relative order.
        # A trailing empty section alone could be hidden by frame.rstrip().
        for index, module in enumerate(modules):
            monkeypatch.setattr(module, "ORDER", index * 10)
        # Compare two frames produced now, never a frozen screen or golden text.
        before = plain_frame(watch_layout.render_frame(snapshot, config, tmp_path))
        write_section(directory, "acceptance_empty", 5)
        after = plain_frame(watch_layout.render_frame(snapshot, config, tmp_path))
        assert after == before, "a section returning [] must not add a separator or blank line"


def test_view_sections_existing_groups_are_modules_used_by_the_compositor(tmp_path, monkeypatch):
    from foragent import watch_layout

    tree = ast.parse(inspect.getsource(watch_layout))
    inline_keys = {node.value for node in ast.walk(tree)
                   if isinstance(node, ast.Constant) and isinstance(node.value, str)} & SECTION_KEYS
    assert not inline_keys, f"watch_layout.py still implements screen sections: {sorted(inline_keys)}"
    snapshot, config = running_snapshot()
    with extra_sections(tmp_path, monkeypatch) as (package, _):
        modules = section_modules(package)
        assert EXISTING_SECTIONS <= {module.__name__.rsplit(".", 1)[-1] for module in modules}
        assert all(type(module.ORDER) is int for module in modules)
        assert len({module.ORDER for module in modules}) == len(modules)
        calls = []
        for module in modules:
            assert callable(module.render)
            assert module.render.__module__ == module.__name__, module.__name__
            source = Path(inspect.getsourcefile(module.render)).resolve()
            assert any(source.is_relative_to(Path(path).resolve()) for path in package.__path__)

            def render(actual_snapshot, actual_config, repo_root, *, _name=module.__name__, **context):
                assert actual_snapshot is snapshot and actual_config is config
                assert repo_root == tmp_path
                assert context["tick"] == 2 and context["width"] == 120
                assert context["selected"] == "work" and context["now"] == snapshot.captured_at
                assert context["layout"] is layout
                calls.append(_name)
                return [[(_name, "")]]

            monkeypatch.setattr(module, "render", render)
        expected = [module.__name__ for module in sorted(modules, key=lambda module: module.ORDER)]
        layout = {}
        frame = plain_frame(watch_layout.render_frame(
            snapshot, config, tmp_path, tick=2, selected="work", now=snapshot.captured_at, layout=layout,
        ))
        assert calls == expected, "render_frame must invoke every discovered section in ORDER"
        replaced = {name for module in modules for name in getattr(module, "REPLACES", ())}
        assert [line for line in frame.splitlines() if line] == [name for name in expected if name not in replaced]
