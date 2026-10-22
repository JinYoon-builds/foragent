"""Acceptance test for design-session. Locked by Foragent; do not edit."""

from datetime import timedelta
import json
from pathlib import Path
import shlex
import subprocess

import yaml

from test_local_truth import record_gh
from test_orch_review_handoff import bind_cli, make_case, tick_context
from test_skills_autosync import cli_exit, isolated_home


class TerminalCommands:
    """Record terminal requests, with a controllable opened-tab lifetime."""

    def __init__(self, monkeypatch, *, tmux=False, claude_reply=None):
        import shutil

        self.calls, self.alive, self.on_create = [], True, None
        for key in ("ORCA_TERMINAL_HANDLE", "TMUX", "TERM_PROGRAM", "WEZTERM_PANE", "KITTY_WINDOW_ID"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("TMUX" if tmux else "ORCA_TERMINAL_HANDLE", "offline-source")
        monkeypatch.setattr(shutil, "which", lambda name: f"/offline/{name}"
                            if name in {"claude", "tmux" if tmux else "orca"} else None)
        original_run, original_popen = subprocess.run, subprocess.Popen

        def run(argv, *args, **kwargs):
            if isinstance(argv, (list, tuple)) and Path(argv[0]).name in {"orca", "tmux", "claude", "osascript"}:
                self.calls.append(list(argv))
                if Path(argv[0]).name == "claude" and claude_reply is not None:
                    assert argv[1] in {"agents", "rm"}, "Claude starts only inside the fake tab"
                    return subprocess.CompletedProcess(argv, 0, stdout=claude_reply(list(argv), kwargs), stderr="")
                assert Path(argv[0]).name in {"orca", "tmux"}, "Only the opener may run; Claude stays in the fake tab"
                if argv[1:3] == ["terminal", "show"] and not self.alive:
                    raise subprocess.CalledProcessError(1, argv, stderr="terminal is gone")
                if argv[1:3] == ["terminal", "create"] and self.on_create is not None:
                    self.on_create()
                output = json.dumps({"handle": "term_design", "terminal": "term_design", "id": "term_design"})
                return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")
            return original_run(argv, *args, **kwargs)

        def popen(argv, *args, **kwargs):
            if isinstance(argv, (list, tuple)) and Path(argv[0]).name in {"orca", "tmux", "claude", "osascript"}:
                result = run(argv, *args, **kwargs)

                class Opened:
                    pid, returncode = 99001, 0

                    def poll(self):
                        return 0

                    def wait(self, timeout=None):
                        return 0

                    def communicate(self, *args, **kwargs):
                        return result.stdout, result.stderr

                return Opened()
            return original_popen(argv, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", run)
        monkeypatch.setattr(subprocess, "Popen", popen)


def design_case(git_history, tmp_path, monkeypatch, *, tmux=False):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true", max_parallel=1)
    case.store.finish_run(1, "fixture-idle")
    case.tracker.remove_labels(1, ["running"])
    main = bind_cli(case, monkeypatch)
    commands = TerminalCommands(monkeypatch, tmux=tmux)
    gh = record_gh(monkeypatch)
    return case, main, commands, gh


def test_design_session_explicit_orca_open_ignores_view_off_and_places_preview_beside_it(
    git_history, tmp_path, monkeypatch,
):
    case, main, commands, gh = design_case(git_history, tmp_path, monkeypatch)
    monkeypatch.setenv("FORAGENT_VIEW", "off")
    assert cli_exit(main, ["design", "open", "--repo-root", str(case.root), "--topic", "lane-view"]) == 0
    tabs = [argv for argv in commands.calls if argv[1:3] == ["terminal", "create"]]
    assert len(tabs) == 1, commands.calls
    tab = tabs[0]
    assert tab[tab.index("--title") + 1] == f"design: {case.root.name}"
    command = shlex.split(tab[tab.index("--command") + 1])
    assert Path(command[0]).name == "claude" and any("fa-design-session" in arg for arg in command)
    assert "lane-view" in " ".join(command)
    splits = [argv for argv in commands.calls if argv[1:3] == ["terminal", "split"]]
    assert len(splits) == 1 and splits[0][splits[0].index("--direction") + 1] == "vertical"
    assert splits[0][splits[0].index("--terminal") + 1] == "term_design"
    assert gh == []


def test_design_session_tmux_open_without_topic_creates_a_new_window_and_returns(
    git_history, tmp_path, monkeypatch,
):
    case, main, commands, gh = design_case(git_history, tmp_path, monkeypatch, tmux=True)
    assert cli_exit(main, ["design", "open", "--repo-root", str(case.root)]) == 0
    windows = [argv for argv in commands.calls if "new-window" in argv]
    assert len(windows) == 1, commands.calls
    assert f"design: {case.root.name}" in windows[0]
    assert "claude" in " ".join(windows[0]) and "fa-design-session" in " ".join(windows[0])
    assert not any("attach-session" in argv or "wait-for" in argv for argv in commands.calls)
    assert gh == []


def test_design_session_is_recorded_outside_active_runs_and_never_stalls_or_occupies_a_lane(
    git_history, tmp_path, monkeypatch,
):
    from foragent.scheduler import tick

    case, main, commands, gh = design_case(git_history, tmp_path, monkeypatch)
    assert cli_exit(main, ["design", "open", "--repo-root", str(case.root)]) == 0
    records = [path.read_text() for path in case.store.root.rglob("*.json")]
    assert any("term_design" in record for record in records), "Keep a durable design-session terminal record"
    assert case.store.active_runs() == []
    case.clock[0] += timedelta(minutes=45)
    case.tracker.record_label(1, "approved", "human")
    report = tick(tick_context(case))
    assert not any("stall" in action for action in report.actions), report.actions
    assert [run.number for run in case.store.active_runs()] == [1], report.actions
    assert commands.alive and not any("rm" in argv for argv in commands.calls)
    assert gh == []


def test_design_session_skill_defines_comparison_tools_durable_drafts_and_handoff():
    path = Path(__file__).resolve().parents[2] / "skills/fa-design-session/SKILL.md"
    assert path.is_file(), "D72 needs the fa-design-session skill"
    source = path.read_text(encoding="utf-8")
    assert source.startswith("---\n")
    assert yaml.safe_load(source.split("---", 2)[1])["name"] == "fa-design-session"
    text = " ".join(source.lower().split())
    groups = {
        "multiple drafts": ("2–3", "2-3", "2~3", "two or three", "2~3개"),
        "comparison": ("side by side", "side-by-side", "나란히"),
        "terminal preview": ("terminal", "터미널"),
        "Orca panes": ("orca",),
        "web preview": ("local server", "로컬 서버"),
        "browser": ("browser", "브라우저"),
        "gstack screenshots": ("gstack",),
        "Playwright": ("playwright",),
        "comparison page": ("comparison page", "비교 페이지"),
        "design-file tool": ("figma",),
        "existing design files": ("design files", "디자인 파일"),
        "mobile preview": ("simulator", "시뮬레이터"),
        "screenshots": ("screenshot", "스크린샷"),
        "find a missing tool": ("find a tool", "find tools", "propose a tool", "찾아", "도구를 찾"),
        "record decisions": ("decision", "결정"),
        "chosen draft": ("chosen draft", "selected draft", "선택한 시안", "고른 시안"),
        "handoff": ("orchestrator", "오케스트레이터"),
        "visual done-when": ("same screen as the draft", "시안과 같은 화면"),
    }
    missing = [meaning for meaning, alternatives in groups.items() if not any(word in text for word in alternatives)]
    assert not missing, f"Missing design-session guidance: {missing}"
    assert "docs/design/<topic>/" in source and "docs/design/<topic>/HANDOFF.md" in source
    assert any(word in text for word in ("never one", "never show only one", "do not show only one", "하나만")), (
        'Explicitly reject showing one draft and asking "how is it?"'
    )


def test_design_session_orchestrator_suggests_design_and_turns_handoff_into_lane_issues():
    path = Path(__file__).resolve().parents[2] / "skills/fa-orchestrator/SKILL.md"
    text = " ".join(path.read_text(encoding="utf-8").lower().split())
    assert "fa design open" in text and "handoff.md" in text, "D72 design-session routing is missing"
    assert any(word in text for word in ("suggest", "offer", "propose", "제안"))
    assert "lane" in text and "issue" in text and "done when" in text
