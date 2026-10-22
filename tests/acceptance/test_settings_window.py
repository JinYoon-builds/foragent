"""Acceptance test for settings-window. Locked by Foragent; do not edit."""

from dataclasses import asdict
import json

import yaml

from test_local_truth import record_gh
from test_orch_review_handoff import bind_cli, make_case
from test_skills_autosync import cli_exit, isolated_home
from test_squirrel_running import plain_frame, running_snapshot, view_offline


def settings_case(git_history, tmp_path, monkeypatch):
    """Settings use team/me/computer tabs and config.set_setting as CLI's writer.

    get --json returns effective config keys. The UI's caller-owned state holds
    settings=True while open; confirm(message) is the team's approval callback.
    """
    from foragent import cli
    from foragent.config import load_config

    case = make_case(git_history, tmp_path, monkeypatch, language="ko", review="batch", skill_libraries=[])
    main = bind_cli(case, monkeypatch)
    (case.root / ".foragent.yml").write_text(yaml.safe_dump(asdict(case.config), allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(cli, "load_config", load_config)
    return case, main


def stored_settings(case, main, capsys):
    capsys.readouterr()
    assert cli_exit(main, ["settings", "get", "--json", "--repo-root", str(case.root)]) == 0
    return json.loads(capsys.readouterr().out)


def test_settings_window_s_opens_three_tabs_with_values_and_one_line_descriptions(
    git_history, tmp_path, monkeypatch,
):
    from foragent.watch import handle_key, settings_view

    case, _ = settings_case(git_history, tmp_path, monkeypatch)
    state = {}
    handle_key("s", case.store, case.config, state=state, repo_root=case.root)
    assert state["settings"] is True
    items = {
        "team": (("머지", "모아서 물어보기"), ("작업자", "claude"), ("리뷰어", "codex")),
        "me": (("언어", "ko"),),
        "computer": (("동시 실행", "2"),),
    }
    for tab, expected in items.items():
        lines = settings_view(case.store, case.config, case.root, tab)
        assert isinstance(lines, list) and all(isinstance(line, str) for line in lines)
        rendered = plain_frame("\n".join(lines))
        assert all(label in rendered for label in ("👥 팀", "🙋 나", "💻 이 컴퓨터"))
        for label, value in expected:
            row = next((line for line in map(plain_frame, lines) if label in line and value in line), "")
            assert row, (tab, label, rendered)
            assert row.partition(value)[2].strip(" │·—:-"), f"{label} needs a one-line description"


def test_settings_window_team_choice_confirms_once_and_uses_the_cli_setter(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent import cli, config as config_module
    from foragent.config import load_config, set_setting
    from foragent.watch import handle_key

    case, main = settings_case(git_history, tmp_path, monkeypatch)
    calls, confirmations = [], []

    def save(*args, **kwargs):
        calls.append((args, kwargs))
        return set_setting(*args, **kwargs)

    monkeypatch.setattr(config_module, "set_setting", save)
    # CLI may import the shared function directly or access its module.
    if hasattr(cli, "set_setting"):
        monkeypatch.setattr(cli, "set_setting", save)
    assert cli_exit(main, ["settings", "set", "review", "fast", "--repo-root", str(case.root)]) == 0
    assert len(calls) == 1 and stored_settings(case, main, capsys)["review"] == "fast"
    config, state = load_config(case.root), {}

    def confirm(message):
        confirmations.append(message)
        # The choice must still be unsaved when the approval is requested.
        assert load_config(case.root).review == "fast"
        return True

    # Review is the first team item; values follow fast -> batch -> careful.
    for key in ("s", "enter", "right", "enter"):
        handle_key(key, case.store, config, state=state, repo_root=case.root, confirm=confirm)
    assert len(confirmations) == 1 and "팀 전체에 적용돼요" in confirmations[0]
    assert len(calls) == 2, "settings window and CLI must call the same public setter"
    assert stored_settings(case, main, capsys)["review"] == "batch"


def test_settings_window_footer_reflects_review_changes(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent.config import load_config
    from foragent.watch import render_frame

    case, main = settings_case(git_history, tmp_path, monkeypatch)
    snapshot, _ = running_snapshot()
    before = plain_frame(render_frame(snapshot, load_config(case.root), case.root, width=160))
    assert "머지: 모아서 물어보기" in before.splitlines()[-1]
    assert cli_exit(main, ["settings", "set", "review", "careful", "--repo-root", str(case.root)]) == 0
    assert stored_settings(case, main, capsys)["review"] == "careful"
    after = plain_frame(render_frame(snapshot, load_config(case.root), case.root, width=160))
    assert "머지: 하나씩" in after.splitlines()[-1]
    assert "머지: 모아서 물어보기" not in after.splitlines()[-1]


def test_settings_window_no_executor_needed_for_personal_changes_or_m_shortcut(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent import executors
    from foragent.config import load_config
    from foragent.watch import handle_key, settings_view

    case, main = settings_case(git_history, tmp_path, monkeypatch)
    called = []

    def forbidden(*args, **kwargs):
        called.append(args)
        raise AssertionError("settings must work with exhausted quota and no LLM")

    monkeypatch.setattr(executors, "executor_for", forbidden)
    calls = record_gh(monkeypatch)
    state = {}
    handle_key("s", case.store, case.config, state=state, repo_root=case.root)
    assert settings_view(case.store, case.config, case.root, "me")
    # Personal settings save without the team confirmation gate.
    assert cli_exit(main, ["settings", "set", "animation", "false", "--repo-root", str(case.root)]) == 0
    assert stored_settings(case, main, capsys)["animation"] is False
    for expected in ("burn", "keep"):
        handle_key("m", case.store, load_config(case.root), state=state, repo_root=case.root)
        assert stored_settings(case, main, capsys)["claude_quota"]["mode"] == expected
    assert called == calls == []
