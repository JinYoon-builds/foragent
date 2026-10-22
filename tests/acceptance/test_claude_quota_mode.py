"""Acceptance test for claude-quota-mode. Locked by Foragent; do not edit."""

from dataclasses import replace

import pytest
import yaml

from test_init import setup
from test_skills_autosync import isolated_home


def test_claude_quota_mode_config_defaults_modes_and_invalid_mode(tmp_path):
    from foragent.config import Config, ConfigError, load_config

    assert Config().claude_quota == {"mode": "keep", "keep_percent": 80}
    path = tmp_path / ".foragent.yml"
    for mode in ("keep", "burn"):
        path.write_text(yaml.safe_dump({"claude_quota": {"mode": mode, "keep_percent": 75}}))
        assert load_config(tmp_path).claude_quota == {"mode": mode, "keep_percent": 75}
    path.write_text("claude_quota: {mode: invalid, keep_percent: 80}\n")
    with pytest.raises(ConfigError, match="mode"):
        load_config(tmp_path)


def test_claude_quota_mode_cli_stores_local_override_without_changing_config(
    git_history, tmp_path, monkeypatch, capsys,
):
    from test_orch_review_handoff import bind_cli, make_case

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[])
    main = bind_cli(case, monkeypatch)
    path = case.root / ".foragent.yml"
    before = path.read_bytes()
    assert main(["quota", "mode", "burn", "--repo-root", str(case.root)]) == 0
    assert path.read_bytes() == before
    capsys.readouterr()
    assert main(["quota", "mode", "--repo-root", str(case.root)]) == 0
    assert "burn" in capsys.readouterr().out.lower()
    from foragent.state import StateStore

    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo, **kw: StateStore(case.store.root)))
    # A fresh StateStore must see the persisted override, not an instance attribute.
    assert main(["quota", "mode", "--repo-root", str(case.root)]) == 0
    assert "burn" in capsys.readouterr().out.lower()
    assert main(["quota", "mode", "keep", "--repo-root", str(case.root)]) == 0
    capsys.readouterr()
    assert main(["quota", "mode", "--repo-root", str(case.root)]) == 0
    assert "keep" in capsys.readouterr().out.lower()


@pytest.mark.parametrize("mode,percent,secondary,allowed", [
    ("keep", 81, 2, False), ("keep", 79, 2, True), ("keep", 6, 81, False), ("burn", 99, 2, True),
])
def test_claude_quota_mode_scheduler_respects_share_and_local_override(
    git_history, tmp_path, monkeypatch, mode, percent, secondary, allowed,
):
    from foragent.scheduler import claude_allowed
    from test_orch_review_handoff import bind_cli
    from test_usage_read import usage_case

    case = usage_case(git_history, tmp_path, monkeypatch, used_percent=percent, secondary_percent=secondary)
    config = replace(case.ctx.config, claude_quota={"mode": "keep", "keep_percent": 80})
    case.ctx.config = config
    main = bind_cli(case, monkeypatch)
    if mode == "burn":
        assert claude_allowed(case.store, config) is False
        assert main(["quota", "mode", "burn", "--repo-root", str(case.root)]) == 0
    assert claude_allowed(case.store, config) is allowed


def test_claude_quota_mode_no_usage_allows_claude(tmp_path):
    from foragent.config import Config
    from foragent.scheduler import claude_allowed
    from foragent.state import StateStore

    assert claude_allowed(StateStore(tmp_path / "state"), Config()) is True


@pytest.mark.parametrize("mode", ["keep", "burn"])
def test_claude_quota_mode_init_flag_writes_requested_mode(setup, monkeypatch, mode):
    from foragent import cli

    monkeypatch.setattr(cli, "init_repo", lambda root, **kw: setup.init(**kw))
    assert cli.main(["init", "--repo-root", str(setup.root), "--approver", "human", "--no-push",
                     "--no-launchd", "--no-board", "--claude-quota", mode]) == 0
    assert yaml.safe_load((setup.root / ".foragent.yml").read_text())["claude_quota"]["mode"] == mode
