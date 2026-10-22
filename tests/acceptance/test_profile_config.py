"""Acceptance test for profile-config. Locked by Foragent; do not edit."""

import json

import pytest
import yaml

from test_skills_autosync import isolated_home


def test_profile_config_native_default_preserves_explicit_ouroboros(tmp_path):
    from foragent.config import Config, load_config

    assert Config().interview == "native"
    (tmp_path / ".foragent.yml").write_text("interview: ouroboros\n")
    assert load_config(tmp_path).interview == "ouroboros"


def test_profile_config_mapping_roundtrips_and_nonmapping_is_rejected(tmp_path):
    from foragent.config import ConfigError, load_config

    profile = {"kind": "daily-app", "stage": "build", "priority": "speed", "confirmed_at": "2026-10-22"}
    path = tmp_path / ".foragent.yml"
    path.write_text(yaml.safe_dump({"profile": profile}))
    assert load_config(tmp_path).profile == profile
    for invalid in ("daily-app", ["daily-app"]):
        path.write_text(yaml.safe_dump({"profile": invalid}))
        with pytest.raises(ConfigError, match="profile"):
            load_config(tmp_path)


def test_profile_config_cli_sets_today_and_context_exposes_profile(git_history, tmp_path, monkeypatch, capsys):
    from datetime import date, datetime

    from foragent import cli
    from foragent.config import load_config
    from test_orch_review_handoff import bind_cli, make_case

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[])
    main = bind_cli(case, monkeypatch)
    monkeypatch.setattr(cli, "load_config", load_config)
    monkeypatch.setattr(cli, "datetime", datetime)
    assert main(["profile", "set", "--kind", "daily-app", "--stage", "build", "--priority", "speed",
                 "--repo-root", str(case.root)]) == 0
    expected = {"kind": "daily-app", "stage": "build", "priority": "speed",
                "confirmed_at": date.today().isoformat()}
    assert yaml.safe_load((case.root / ".foragent.yml").read_text())["profile"] == expected
    capsys.readouterr()
    assert main(["order", "context", "--json", "--repo-root", str(case.root)]) == 0
    assert json.loads(capsys.readouterr().out)["profile"] == expected


def test_profile_config_unset_profile_is_null_in_context(git_history, tmp_path, monkeypatch, capsys):
    from test_orch_review_handoff import bind_cli, make_case

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[])
    main = bind_cli(case, monkeypatch)
    assert main(["order", "context", "--json", "--repo-root", str(case.root)]) == 0
    assert json.loads(capsys.readouterr().out)["profile"] is None
