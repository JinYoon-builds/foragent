"""Acceptance test for messages-registry. Locked by Foragent; do not edit."""

import ast
from contextlib import contextmanager
import importlib
import importlib.util
import inspect
import os
import pkgutil
import signal
from string import Formatter
import sys

import pytest

from test_skills_autosync import isolated_home
from test_squirrel_running import view_offline


# Both languages had these 57 keys on main at 707ff7b. Freeze names only:
# later features may add keys and change any wording or format placeholders.
_KEY_NAMES = frozenset("""
    summary status_unknown status_as_of working blocked waiting-human queued idle
    implementing checking reviewing ci merging resolving-conflict elapsed activity
    waiting_for watch_orchestrator watch_request_bubble watch_requests
    watch_monitoring watch_handling watch_order watch_no_order watch_waiting
    watch_eta watch_estimating watch_complete watch_lanes watch_agent
    watch_idle_lanes watch_all_idle watch_done watch_stalled watch_runner_gone
    watch_now watch_lane watch_phase watch_events watch_no_events watch_retry
    watch_detail watch_paths watch_executor watch_model watch_skills
    watch_done_count watch_memory watch_no_memory watch_default_model watch_none
    watch_help watch_help_many watch_terminal watch_open_failed watch_open_manual
    watch_refresh_failed
""".split())
LEGACY_KEYS = {"en": _KEY_NAMES, "ko": _KEY_NAMES}


@pytest.fixture(autouse=True)
def registry_offline(view_offline, monkeypatch):
    """Reuse HOME/I/O isolation, and prohibit processes and signal delivery too."""
    import subprocess

    def forbidden(*args, **kwargs):
        pytest.fail("messages-registry acceptance must not run processes or send signals")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr(signal, "raise_signal", forbidden)


@contextmanager
def extra_messages(tmp_path, monkeypatch):
    """Public reset: i18n.reload_messages() rediscovers package MESSAGES mappings.

    Duplicate (language, key) entries raise ValueError with the key and both
    qualified module names. The same key in different languages is valid.
    Only tmp_path receives source files; restore paths, imports and merged state.
    """
    from foragent import i18n

    assert importlib.util.find_spec("foragent.messages") is not None, (
        "missing discoverable package foragent.messages"
    )
    package = importlib.import_module("foragent.messages")
    assert callable(getattr(i18n, "reload_messages", None)), (
        "i18n.reload_messages() must publicly reset and rediscover the registry"
    )
    directory = tmp_path / "message-features"
    directory.mkdir()
    before = set(sys.modules)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(package, "__path__", [*package.__path__, str(directory)])
            importlib.invalidate_caches()
            yield package, i18n, directory
    finally:
        for name in set(sys.modules) - before:
            if name.startswith(package.__name__ + "."):
                module = sys.modules.pop(name)
                child = name.rsplit(".", 1)[-1]
                if getattr(package, child, None) is module:
                    delattr(package, child)
        importlib.invalidate_caches()
        i18n.reload_messages()


def write_messages(directory, name, messages):
    (directory / f"{name}.py").write_text(f"MESSAGES = {messages!r}\n", encoding="utf-8")
    importlib.invalidate_caches()


@pytest.mark.parametrize("language", ["en", "ko"])
def test_messages_registry_discovers_appended_modules_without_a_central_list(
    tmp_path, monkeypatch, language,
):
    with extra_messages(tmp_path, monkeypatch) as (_, i18n, directory):
        # Warm any cache before introducing either feature, then discover twice.
        i18n.reload_messages()
        for feature in ("acceptance_z_feature", "acceptance_a_later_feature"):
            key = feature + "_message"
            translations = {"en": {key: "feature {number}"}, "ko": {key: "기능 {number}"}}
            write_messages(directory, feature, translations)
            i18n.reload_messages()
            assert i18n.text(key, language, number=23) == translations[language][key].format(number=23)
            assert i18n.MESSAGES[language][key] == translations[language][key]


@pytest.mark.parametrize("language", ["en", "ko"])
def test_messages_registry_duplicate_key_names_both_owning_modules(
    tmp_path, monkeypatch, language,
):
    with extra_messages(tmp_path, monkeypatch) as (package, i18n, directory):
        key = "acceptance_duplicate_message"
        names = ("acceptance_duplicate_a", "acceptance_duplicate_b")
        for name in names:
            translations = {"en": {}, "ko": {}}
            translations[language][key] = "same value"
            write_messages(directory, name, translations)
        with pytest.raises(ValueError) as error:
            i18n.reload_messages()
        message = str(error.value)
        assert key in message, message
        assert all(f"{package.__name__}.{name}" in message for name in names), message


def test_messages_registry_preserves_frozen_keys_in_discovered_feature_catalogues(
    tmp_path, monkeypatch,
):
    from foragent import i18n

    tree = ast.parse(inspect.getsource(i18n))
    inline_keys = {
        key.value for node in ast.walk(tree) if isinstance(node, ast.Dict)
        for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)
    } & _KEY_NAMES
    assert not inline_keys, f"i18n.py still defines message keys inline: {sorted(inline_keys)}"
    with extra_messages(tmp_path, monkeypatch) as (package, i18n, _):
        i18n.reload_messages()
        catalogues = [
            importlib.import_module(info.name).MESSAGES
            for info in pkgutil.iter_modules(package.__path__, package.__name__ + ".")
            if not info.ispkg
        ]
        assert catalogues, "existing messages must live in discoverable feature modules"
        for language, keys in LEGACY_KEYS.items():
            assert keys <= i18n.MESSAGES[language].keys()
            for key in sorted(keys):
                owners = [catalogue[language][key] for catalogue in catalogues
                          if key in catalogue.get(language, {})]
                assert len(owners) == 1, (language, key, owners)
                template = i18n.MESSAGES[language][key]
                assert template == owners[0]
                values = {field: 7 for _, field, _, _ in Formatter().parse(template) if field}
                assert i18n.text(key, language, **values) == template.format(**values)


def test_messages_registry_keeps_language_fallback_and_formatting_behaviour(
    tmp_path, monkeypatch,
):
    with extra_messages(tmp_path, monkeypatch) as (_, i18n, directory):
        parameters = inspect.signature(i18n.text).parameters
        assert list(parameters) == ["key", "language", "values"]
        assert parameters["key"].kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
        assert parameters["language"].kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
        assert parameters["language"].default == "en"
        assert parameters["values"].kind == inspect.Parameter.VAR_KEYWORD
        shared, english_only = "acceptance_shared_message", "acceptance_english_only"
        write_messages(directory, "acceptance_fallback", {
            "en": {shared: "English {value} {{literal}}", english_only: "English only"},
            "ko": {shared: "한국어 {value} {{literal}}"},
        })
        i18n.reload_messages()
        assert i18n.text(shared, value="sample") == "English sample {literal}"
        for language in ("en", "EN-us", "fr-FR", "", "ko_KR"):
            assert i18n.text(shared, language, value="sample") == "English sample {literal}"
        for language in ("ko", "KO-kr"):
            assert i18n.text(shared, language, value="예시", unused="ignored") == "한국어 예시 {literal}"
        assert i18n.text(english_only, "fr") == "English only"
        # Today's fallback selects a language; it does not fall back per key.
        with pytest.raises(KeyError) as error:
            i18n.text(english_only, "ko")
        assert error.value.args == (english_only,)
        with pytest.raises(KeyError) as error:
            i18n.text("acceptance_unknown_message", "fr")
        assert error.value.args == ("acceptance_unknown_message",)
        with pytest.raises(KeyError) as error:
            i18n.text(shared, "en")
        assert error.value.args == ("value",)
