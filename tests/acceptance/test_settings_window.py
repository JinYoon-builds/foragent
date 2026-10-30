"""Acceptance test for settings-window. Locked by Foragent; do not edit."""

from dataclasses import asdict
import json

import yaml

from test_local_truth import record_gh
from test_orch_review_handoff import bind_cli, make_case
from test_skills_autosync import cli_exit, isolated_home
from test_squirrel_running import issue_row, plain_frame, running_snapshot, view_offline


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


def settings_api(*names):
    from foragent import watch

    for name in names:
        assert callable(getattr(watch, name, None)), f"the public lane-view settings API must expose {name}"
    return tuple(getattr(watch, name) for name in names)


def readable_settings(case):
    """The shared helper's dump includes None; load_config requires omitted optionals.

    Keep settings_case unchanged for importers; normalize only these tests' files.
    """
    path = case.root / ".foragent.yml"
    values = yaml.safe_load(path.read_text(encoding="utf-8"))
    path.write_text(yaml.safe_dump({k: v for k, v in values.items() if v is not None},
                                   allow_unicode=True), encoding="utf-8")


def test_settings_window_s_opens_three_tabs_with_values_and_one_line_descriptions(
    git_history, tmp_path, monkeypatch, capsys,
):
    handle_key, settings_view = settings_api("handle_key", "settings_view")

    case, main = settings_case(git_history, tmp_path, monkeypatch)
    readable_settings(case)
    state = {}
    handle_key("s", case.store, case.config, state=state, repo_root=case.root)
    assert state["settings"] is True
    capsys.readouterr()
    assert cli_exit(main, ["settings", "list", "--json", "--repo-root", str(case.root)]) == 0
    listed = json.loads(capsys.readouterr().out)
    glyphs = next((item for item in listed if item["key"] == "view_glyphs"), None)
    assert glyphs is not None, "settings must list view_glyphs"
    assert glyphs["scope"] == "me" and set(glyphs["choices"]) == {"auto", "emoji", "ascii"}
    assert glyphs["description"].strip(), "view_glyphs needs a one-line description"
    assert stored_settings(case, main, capsys)["view_glyphs"] == "auto"
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
        if tab == "me":
            glyph_rows = [line for line in map(plain_frame, lines) if "auto" in line or "자동" in line]
            assert glyph_rows, "the personal tab must show the default glyph choice"
            assert any(row.partition("auto" if "auto" in row else "자동")[2].strip(" │·—:-") for row in glyph_rows), "the glyph choice needs a one-line description"
        for label, value in expected:
            row = next((line for line in map(plain_frame, lines) if label in line and value in line), "")
            assert row, (tab, label, rendered)
            assert row.partition(value)[2].strip(" │·—:-"), f"{label} needs a one-line description"


def test_settings_window_team_choice_confirms_once_and_uses_the_cli_setter(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent import cli, config as config_module
    from foragent.config import load_config
    assert callable(getattr(config_module, "set_setting", None)), "CLI and settings window must share config.set_setting"
    set_setting = config_module.set_setting
    handle_key, = settings_api("handle_key")

    case, main = settings_case(git_history, tmp_path, monkeypatch)
    readable_settings(case)
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
    from foragent.watch import render_frame,handle_key
    case,main=settings_case(git_history,tmp_path,monkeypatch)
    readable_settings(case)
    snapshot,_=running_snapshot()
    state={'store':case.store}
    handle_key('s',case.store,load_config(case.root),state=state,repo_root=case.root)
    before=plain_frame(render_frame(snapshot,load_config(case.root),case.root,width=160,state=state))
    assert '모아서 물어보기' in before
    assert cli_exit(main,['settings','set','review','careful','--repo-root',str(case.root)])==0
    assert stored_settings(case,main,capsys)['review']=='careful'
    after=plain_frame(render_frame(snapshot,load_config(case.root),case.root,width=160,state=state))
    assert '하나씩' in after and '모아서 물어보기' not in after
    assert before.splitlines()[-1]==after.splitlines()[-1]=='도토리를 누르면 설명해요 · s 설정 · m 사용량 모드 · q 끝내기'


def test_settings_window_no_executor_needed_for_personal_changes_or_m_shortcut(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent import executors
    from foragent.config import load_config
    handle_key, settings_view = settings_api("handle_key", "settings_view")

    case, main = settings_case(git_history, tmp_path, monkeypatch)
    readable_settings(case)
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
    assert cli_exit(main, ["settings", "set", "view_glyphs", "ascii", "--repo-root", str(case.root)]) == 0
    assert stored_settings(case, main, capsys)["view_glyphs"] == "ascii"
    choices = plain_frame("\n".join(settings_view(case.store, load_config(case.root), case.root, "me")))
    assert "ascii" in choices, "the personal settings tab must show the saved glyph choice"
    snapshot, _ = running_snapshot()
    from foragent.watch import render_frame
    rendered = plain_frame(render_frame(snapshot, load_config(case.root), case.root, width=100))
    assert "#101" in issue_row(rendered, 101), "personal ASCII choice keeps the issue readable"
    assert "▀" not in rendered and "▄" not in rendered
    for expected in ("burn", "keep"):
        handle_key("m", case.store, load_config(case.root), state=state, repo_root=case.root)
        assert stored_settings(case, main, capsys)["claude_quota"]["mode"] == expected
    assert called == calls == []

