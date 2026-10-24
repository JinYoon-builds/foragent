"""Acceptance test for settings-runner-tests. Locked by Foragent; do not edit."""

import json

import yaml

from test_settings_window import settings_case, stored_settings
from test_skills_autosync import cli_exit, isolated_home
from test_squirrel_running import plain_frame


# D81 item 3, on the D70 team tab (Korean copy, as in test_settings_window.py).
KEY = "runner_unit_tests"  # .foragent.yml: auto (default) | ci | all
LABEL = "단위 시험"  # "러너가 돌리는 단위 시험"
CHOICE_LABELS = {"ci": "CI에 맡기기", "all": "전부 돌리기"}
AUTO = "자동"
PR_WORKFLOW = "on:\n  pull_request:\n  push:\n    branches: [main]\njobs: {}\n"


def add_pr_ci(root):
    workflow = root / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text(PR_WORKFLOW, encoding="utf-8")


def runner_tests_row(case):
    from foragent.config import load_config
    from foragent.watch import settings_view

    lines = [plain_frame(line) for line in settings_view(case.store, load_config(case.root), case.root, "team")]
    rows = [line for line in lines if LABEL in line]
    assert len(rows) == 1, "\n".join(lines)
    return rows[0]


def team_items(case, main, capsys):
    capsys.readouterr()
    assert cli_exit(main, ["settings", "list", "--json", "--repo-root", str(case.root)]) == 0
    return [item for item in json.loads(capsys.readouterr().out) if item["scope"] == "team"]


def test_settings_runner_tests_team_item_shows_the_automatic_choice_in_effect(
    git_history, tmp_path, monkeypatch, capsys,
):
    case, main = settings_case(git_history, tmp_path, monkeypatch)
    item = next((item for item in team_items(case, main, capsys) if item["key"] == KEY), None)
    assert item is not None, f"`{KEY}` must be a team setting"
    assert {"ci", "all"} <= set(item["choices"]) and item["description"].strip()
    assert stored_settings(case, main, capsys)[KEY] == "auto"

    # No pull_request workflow: automatic means the runner runs every related test.
    without_ci = runner_tests_row(case)
    assert AUTO in without_ci and CHOICE_LABELS["all"] in without_ci, without_ci
    add_pr_ci(case.root)
    row = runner_tests_row(case)
    assert AUTO in row and CHOICE_LABELS["ci"] in row, row
    assert row != without_ci, "The row must show which choice the automatic setting applies"
    assert row.partition(CHOICE_LABELS["ci"])[2].strip(" │·—:-()"), "The item needs a one-line description"


def test_settings_runner_tests_explicit_choice_overrides_detection_in_the_view(
    git_history, tmp_path, monkeypatch, capsys,
):
    case, main = settings_case(git_history, tmp_path, monkeypatch)
    add_pr_ci(case.root)
    automatic = runner_tests_row(case)
    assert cli_exit(main, ["settings", "set", KEY, "all", "--repo-root", str(case.root)]) == 0
    assert stored_settings(case, main, capsys)[KEY] == "all"
    assert yaml.safe_load((case.root / ".foragent.yml").read_text(encoding="utf-8"))[KEY] == "all"
    row = runner_tests_row(case)
    assert CHOICE_LABELS["all"] in row and row != automatic, (automatic, row)
    assert cli_exit(main, ["settings", "set", KEY, "sometimes", "--repo-root", str(case.root)]) != 0
    assert stored_settings(case, main, capsys)[KEY] == "all"


def test_settings_runner_tests_window_writes_the_team_key_and_keeps_other_settings(
    git_history, tmp_path, monkeypatch, capsys,
):
    """Team rows follow `fa settings list` team order; `down` moves one row (review is row 0)."""
    from foragent.config import load_config
    from foragent.watch import handle_key

    case, main = settings_case(git_history, tmp_path, monkeypatch)
    add_pr_ci(case.root)
    team = case.root / ".foragent.yml"
    before = yaml.safe_load(team.read_text(encoding="utf-8"))
    index = [item["key"] for item in team_items(case, main, capsys)].index(KEY)
    confirmations, state = [], {}

    def confirm(message):
        confirmations.append(message)
        return True

    handle_key("s", case.store, load_config(case.root), state=state, repo_root=case.root, confirm=confirm)
    for _ in range(index):
        handle_key("down", case.store, load_config(case.root), state=state, repo_root=case.root, confirm=confirm)
    # Step through the choices (order is the implementation's) until "run all" is saved.
    for _ in range(3):
        for key in ("enter", "right", "enter"):
            handle_key(key, case.store, load_config(case.root), state=state, repo_root=case.root, confirm=confirm)
        if yaml.safe_load(team.read_text(encoding="utf-8")).get(KEY) == "all":
            break
    after = yaml.safe_load(team.read_text(encoding="utf-8"))
    assert after.get(KEY) == "all", after.get(KEY)
    assert confirmations and all("팀 전체에 적용돼요" in message for message in confirmations)
    assert {key: value for key, value in after.items() if key != KEY} == {
        key: value for key, value in before.items() if key != KEY
    }, "Changing the runner tests item must keep every other team setting"
    assert CHOICE_LABELS["all"] in runner_tests_row(case)
