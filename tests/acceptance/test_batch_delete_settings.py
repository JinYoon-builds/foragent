"""Locked S2 acceptance: CLI settings expose only supported review choices."""

import json

from test_order_progress import offline_local_cli
from test_settings_cmd import file_bytes, settings_command
from test_skills_autosync import isolated_home  # noqa: F401


def test_settings_rejects_batch_atomically_and_names_valid_choices(tmp_path, monkeypatch, capsys):
    """Reject a CLI that still writes batch even when the config loader has retired it."""
    case = offline_local_cli(tmp_path, monkeypatch)
    assert settings_command(case, "set", "review", "careful") == 0
    before = file_bytes(tmp_path)
    capsys.readouterr()
    code = settings_command(case, "set", "review", "batch")
    output = capsys.readouterr()
    assert code != 0, "batch is a removed review value"
    assert "review" in output.out + output.err
    assert "fast, careful" in output.out + output.err
    assert file_bytes(tmp_path) == before, "A rejected setting must leave every scope unchanged"
    assert case.gh_calls == []


def test_settings_lists_only_the_two_remaining_review_choices(tmp_path, monkeypatch, capsys):
    """Reject stale batch defaults/choices in the settings catalog used by CLI and UI."""
    case = offline_local_cli(tmp_path, monkeypatch)
    assert settings_command(case, "list", "--json") == 0
    review = next(item for item in json.loads(capsys.readouterr().out) if item["key"] == "review")
    assert review["choices"] == ["fast", "careful"]
    assert review["value"] == review["default"] == "fast"
    assert case.gh_calls == []
