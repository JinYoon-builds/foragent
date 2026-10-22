"""Acceptance: `interview` config and the resolved engine in `fa order context`."""

import json
from pathlib import Path

import pytest

from foragent import order
from foragent.config import ConfigError, load_config
from foragent.tracker import FakeTracker


def context(root: Path, home: Path, config_text: str, plugins: dict | None) -> dict:
    (root / ".foragent.yml").write_text(config_text, encoding="utf-8")
    if plugins is not None:
        path = home / ".claude/plugins/installed_plugins.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 2, "plugins": plugins}), encoding="utf-8")
    return order.context(root, load_config(root, home / "none.yml"), FakeTracker())["interview"]


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    root, home = tmp_path / "repo", tmp_path / "home"
    root.mkdir()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return root, home


OUROBOROS = {"ouroboros@ouroboros": [{"scope": "user", "version": "0.52.0"}]}


def test_requested_ouroboros_runs_when_installed(dirs):
    # D58: native is the default; Ouroboros stays available when requested.
    result = context(*dirs, "review: batch\ninterview: ouroboros\n", OUROBOROS)
    assert result["requested"] == "ouroboros"
    assert result["engine"] == "ouroboros"
    assert result["installed_version"] == "0.52.0"


def test_requested_ouroboros_falls_back_to_native_when_not_installed(dirs):
    result = context(*dirs, "review: batch\ninterview: ouroboros\n", None)
    assert result["requested"] == "ouroboros"
    assert result["engine"] == "native"
    assert result["installed_version"] is None
    assert "not installed" in result["reason"]


def test_falls_back_when_installed_version_differs_from_pin(dirs):
    result = context(*dirs, "interview: ouroboros\nouroboros_version: 0.51.1\n", OUROBOROS)
    assert result["engine"] == "native"
    assert result["pinned_version"] == "0.51.1"
    assert "0.51.1" in result["reason"] and "0.52.0" in result["reason"]


def test_native_requested_stays_native(dirs):
    result = context(*dirs, "interview: native\n", OUROBOROS)
    assert result["requested"] == "native"
    assert result["engine"] == "native"


def test_unknown_engine_is_rejected(dirs):
    root, home = dirs
    (root / ".foragent.yml").write_text("interview: magic\n", encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        load_config(root, home / "none.yml")
    assert "interview" in str(error.value)
    assert "ouroboros" in str(error.value) and "native" in str(error.value)
