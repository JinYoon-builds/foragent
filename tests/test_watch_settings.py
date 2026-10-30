"""Unit tests for the lane view's settings window and `fa settings get` without a key (#173)."""

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from foragent import cli, config as config_module
from foragent.config import ConfigError, load_config
from foragent.state import StateStore
from foragent.watch import handle_key, render_frame, settings_view

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))
from test_view_done_state import done_snapshot  # noqa: E402


@pytest.fixture
def root(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".foragent.yml").write_text("repo: owner/repo\nskill_libraries: []\nlanguage: ko\n")
    monkeypatch.chdir(project)
    return project


@pytest.fixture
def store(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state")
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo, **kw: store))
    return store


def team(root):
    return yaml.safe_load((root / ".foragent.yml").read_text())


def press(keys, root, store, state, **kwargs):
    for key in keys:
        handle_key(key, store, load_config(root), state=state, repo_root=root, **kwargs)


def get_all(root, capsys, *flags):
    capsys.readouterr()
    assert cli.main(["settings", "get", *flags, "--repo-root", str(root)]) == 0
    return capsys.readouterr().out


def test_get_without_key_prints_every_setting_nested_or_dotted(root, store, capsys):
    values = json.loads(get_all(root, capsys, "--json"))
    assert values["claude_quota"] == {"mode": "keep"} and values["view_glyphs"] == "auto"
    assert "claude_quota.mode" not in values and values["max_parallel"] is None
    store.set_claude_quota_mode("burn")
    assert json.loads(get_all(root, capsys, "--json"))["claude_quota"]["mode"] == "burn"
    lines = get_all(root, capsys).splitlines()
    assert "claude_quota.mode burn" in lines and "max_parallel -" in lines and "always_human -" in lines
    assert len(lines) == 14 and "runner_unit_tests auto" in lines


def test_live_team_change_asks_in_the_window_and_n_reverts(root, store):
    state = {}
    press(["s", "right", "enter"], root, store, state)
    assert state["settings_prompt"] == ("review", "careful")
    assert "팀 전체에 적용돼요" in "\n".join(settings_view(store, load_config(root), root, "team", state))
    assert "review" not in team(root), "nothing is saved before y"
    press(["x"], root, store, state)  # Any key but y counts as n.
    assert state["settings_prompt"] is None and state["settings_shown"] is None
    assert "review" not in team(root)
    press(["right", "enter", "y"], root, store, state)
    assert team(root)["review"] == "careful" and state.pop("config_changed") is True


def test_unchanged_enter_saves_nothing_and_personal_rows_skip_confirmation(root, store, monkeypatch):
    calls = []
    real = config_module.set_setting
    monkeypatch.setattr(config_module, "set_setting", lambda *a, **k: calls.append(a) or real(*a, **k))
    state = {}
    press(["s", "enter"], root, store, state)
    assert calls == []
    # 나 tab: claude_quota.mode is the first row; it saves to the user config without asking.
    press(["2", "right", "enter"], root, store, state, confirm=lambda message: pytest.fail("asked"))
    assert len(calls) == 1 and load_config(root).claude_quota["mode"] == "burn"
    assert config_module._global_path().is_file()


def test_read_only_row_and_setter_error_show_one_line_and_save_nothing(root, store, monkeypatch):
    state = {}
    press(["s", "down", "enter"], root, store, state)  # always_human
    assert "fa settings set always_human" in state["settings_message"]

    def refuse(*args, **kwargs):
        raise ConfigError("refused\nfor a reason")

    monkeypatch.setattr(config_module, "set_setting", refuse)
    press(["up", "right", "enter"], root, store, state, confirm=lambda message: True)
    assert state["settings_message"] == "refused for a reason"
    assert "review" not in team(root) and "config_changed" not in state


def test_window_takes_keys_until_closed_and_tabs_follow_glyphs(root, store):
    state = {}
    press(["s", "a", "f", "esc"], root, store, state)
    assert state["settings"] is False and "unfolded" not in state
    press(["s", "tab", "tab"], root, store, state)
    assert state["settings_tab"] == "computer"
    press(["btab"], root, store, state)
    assert state["settings_tab"] == "me"
    computer = settings_view(store, load_config(root), root, "computer", state)
    assert any("동시 실행" in line and "2 (자동)" in line for line in computer)
    (root / ".foragent.yml").write_text("repo: owner/repo\nskill_libraries: []\nlanguage: ko\nview_glyphs: ascii\n")
    tabs = settings_view(store, load_config(root), root, "team", state)[0]
    assert "팀" in tabs and "👥" not in tabs and "💻" not in tabs
    assert handle_key("q", store, load_config(root), state=state, repo_root=root) is True


def test_m_flips_the_local_override_and_does_nothing_without_a_store(root, store):
    state = {"settings": True, "settings_tab": "me"}
    handle_key("m", store, load_config(root), state=state, repo_root=root)
    assert store.claude_quota_mode() == "burn"
    handle_key("m", None, load_config(root), state={}, repo_root=root)
    assert store.claude_quota_mode() == "burn"
    note = settings_view(store, load_config(root), root, "me", state)
    assert any("fa quota mode" in line for line in note)


def test_open_window_replaces_the_rows_between_header_and_help(root, store):
    snapshot, config = done_snapshot()
    config = replace(config, review="careful")
    closed = render_frame(snapshot, config, root, width=120)
    assert "머지: 하나씩" in closed.splitlines()[-1]
    state = {"store": store}
    handle_key("s", store, config, state=state, repo_root=root)
    opened = render_frame(snapshot, config, root, width=120, state=state)
    assert "👥 팀" in opened and "머지: 하나씩" not in opened.splitlines()[-1]
    calm = render_frame(snapshot, replace(config, animation=False), root, width=120)
    assert calm.splitlines()[0] == opened.splitlines()[0], "the header stays, in its one-line form"
    assert "Enter" in opened.splitlines()[-1] or any("Enter" in line for line in opened.splitlines()[-3:])


def test_personal_glyph_choice_wins_over_a_repository_auto(root):
    (root / ".foragent.yml").write_text("repo: owner/repo\nskill_libraries: []\nview_glyphs: auto\n")
    config_module.set_setting(root, "view_glyphs", "ascii", language="en")
    assert load_config(root).view_glyphs == "ascii"
    (root / ".foragent.yml").write_text("repo: owner/repo\nskill_libraries: []\nview_glyphs: emoji\n")
    assert load_config(root).view_glyphs == "emoji", "an explicit repository choice still wins"


def test_get_json_reads_each_scope_from_its_own_file_with_the_quota_override(root, store, capsys):
    (root / ".foragent.yml").write_text("repo: owner/repo\nskill_libraries: []\nanimation: true\n"
                                        "claude_quota: {mode: keep}\n")
    config_module.set_setting(root, "animation", "false", language="en")
    config_module.set_setting(root, "claude_quota.mode", "burn", language="en")
    values = json.loads(get_all(root, capsys, "--json"))
    assert values["animation"] is False, "the me scope's own file, not the repository's value"
    assert values["claude_quota"]["mode"] == "burn"
    store.set_claude_quota_mode("keep")
    assert json.loads(get_all(root, capsys, "--json"))["claude_quota"]["mode"] == "keep"


def test_reviewer_cycles_claude_codex_unset_none_with_their_labels(root, store):
    state = {}
    press(["s", "down", "down", "down"], root, store, state)  # reviewer, unset in this repository
    view = lambda: "\n".join(settings_view(store, load_config(root), root, "team", state))
    assert "다른 도구가 리뷰" in view()
    press(["right"], root, store, state)
    assert "리뷰 안 함" in view()
    press(["enter", "y"], root, store, state)
    assert team(root)["reviewer"] == "none"
    for expected in ("claude", "codex"):
        press(["right", "enter", "y"], root, store, state)
        assert team(root)["reviewer"] == expected
    press(["right", "enter", "y"], root, store, state)
    assert "reviewer" not in team(root), "unset clears the key"
    assert "다른 도구가 리뷰" in view()


def test_note_names_the_repository_even_when_it_matches_the_user_value(root, store):
    (root / ".foragent.yml").write_text("repo: owner/repo\nskill_libraries: []\nlanguage: ko\n"
                                        "animation: true\nview_glyphs: auto\n")
    config_module.set_setting(root, "animation", "true", language="en")
    config_module.set_setting(root, "view_glyphs", "ascii", language="en")
    lines = settings_view(store, load_config(root), root, "me", {})
    after = lambda label: lines[next(i for i, line in enumerate(lines) if label in line) + 1]
    assert ".foragent.yml" in after("애니메이션"), "presence in .foragent.yml decides the note"
    assert ".foragent.yml" not in after("화면 기호"), "a repository auto yields to the person's glyphs"


def test_prompt_counts_unmapped_keys_as_n_but_not_timeouts_or_resizes():
    from foragent.watch_terminal import window_key

    names, not_keys = {ord("y"): "y"}, (-1, 410, 409)
    assert window_key(ord(" "), names, True, not_keys) == "other"
    assert window_key(ord(" "), names, False, not_keys) is None
    assert window_key(ord("y"), names, True, not_keys) == "y"
    assert all(window_key(key, names, True, not_keys) is None for key in not_keys)


def test_short_screen_scrolls_to_the_selected_row_and_the_prompt(root, store):
    snapshot, config = done_snapshot()
    state = {"store": store}
    press(["s", "down", "down", "down", "right", "enter"], root, store, state)
    screen = render_frame(snapshot, load_config(root), root, width=80, height=12, state=state)
    assert "› 리뷰어" in screen and "팀 전체에 적용돼요" in screen
    press(["n", "up", "up", "up"], root, store, state)
    screen = render_frame(snapshot, load_config(root), root, width=80, height=12, state=state)
    assert "› 머지" in screen


@pytest.mark.parametrize("width,height", [(80, 12), (40, 12), (80, 8)])
def test_short_screen_keeps_header_tabs_prompt_and_help_around_the_window(root, store, width, height):
    snapshot, _ = done_snapshot()
    state = {"store": store}
    press(["s", "down", "down", "down", "right", "enter"], root, store, state)
    lines = render_frame(snapshot, load_config(root), root, width=width, height=height, state=state).splitlines()
    assert len(lines) <= height
    closed = render_frame(snapshot, replace(load_config(root), animation=False), root, width=width).splitlines()
    assert lines[0] == closed[0], "the header stays first, in its one-line form"
    assert lines[-1].startswith("1–2: 담당 보기"), "the help line stays last"
    text = "\n".join(lines)
    assert "팀" in text.split("\n")[2] and "› 리뷰어" in text and "팀 전체에 적용돼요" in text
    assert "위에" in text and "더 있어요" in text, "the hidden rows are named"


def test_quota_save_shows_on_the_last_line_before_and_after_the_next_snapshot(root, store):
    from foragent.watch import _Refresh

    snapshot, _ = done_snapshot()
    snapshot.meter["mode"] = "keep"  # Read before the save.
    state = {"store": store}
    press(["s", "2", "right", "enter"], root, store, state)
    assert load_config(root).claude_quota["mode"] == "burn"
    handle_key("s", store, load_config(root), state=state, repo_root=root)
    last = render_frame(snapshot, load_config(root), root, width=120, state=state).splitlines()[-1]
    assert "burn" in last and "keep 80%" not in last
    seen = []
    refresh = _Refresh(seen.append, "old")
    refresh.config = "saved"
    refresh()
    assert seen == ["saved"], "later snapshots read the saved configuration"


def test_runner_unit_tests_automatic_names_the_choice_that_applies(tmp_path):
    from foragent.watch_sections.settings import runner_unit_tests as row

    assert row.value_text("auto", tmp_path, "en") == "automatic (run all)"
    workflow = tmp_path / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("on: [pull_request]\njobs: {}\n", encoding="utf-8")
    assert row.value_text("auto", tmp_path, "en") == "automatic (leave to CI)"
    assert row.value_text("auto", tmp_path, "ko") == "자동 (CI에 맡기기)"
    assert row.value_text("auto", None, "en") == "automatic (run all)"
    assert row.value_text("ci", tmp_path, "en") is None, "explicit choices use the window's usual word"


def test_settings_window_draws_the_runner_row_through_its_own_module(root, store, monkeypatch):
    from foragent.watch_sections.settings import runner_unit_tests as row

    monkeypatch.setattr(row, "value_text", lambda value, repo_root, language: "FROM-ROW-MODULE")
    lines = settings_view(store, load_config(root), root, "team")
    assert any("FROM-ROW-MODULE" in line for line in lines), lines
