"""Acceptance test for cli-split. Locked by Foragent; do not edit."""

import argparse
import ast
import importlib
import importlib.util
import json
from pathlib import Path
import pkgutil
import sys
from types import SimpleNamespace

import pytest

from test_skills_autosync import cli_exit, isolated_home
from test_state_split import offline_acceptance


# Frozen from src/foragent/*.py on main at 28c8bc2, before the CLI split.
BASE_MODULES = (
    "__init__.py", "actions.py", "adopt.py", "bench.py", "board.py", "board_fake.py",
    "brief.py", "capacity.py", "cli.py", "config.py", "design.py", "design_session.py",
    "executors.py", "git.py", "i18n.py", "init.py", "inspect_actions.py", "inspect_data.py",
    "inspect_runtime.py", "issue.py", "kick.py", "labels.py", "lanes.py", "launchd.py",
    "launcher.py", "license_texts.py", "notify.py", "orch.py", "orch_events.py",
    "orch_github.py", "orch_inbox.py", "orch_queue.py", "orch_reinstall.py",
    "orch_tick_outcome.py", "order.py", "paths.py", "policy.py", "processes.py",
    "reasons.py", "records.py", "run_attempt.py", "run_base_move.py", "run_baseline.py",
    "run_checks.py", "run_conflicts.py", "run_execution.py", "run_implementation.py",
    "run_lane_skills.py", "run_prompts.py", "run_publish.py", "run_review.py",
    "run_waits.py", "runner.py", "scheduler.py", "scheduler_approval.py",
    "scheduler_checks.py", "scheduler_lanes.py", "scheduler_recovery.py",
    "scheduler_start.py", "scheduler_stop.py", "skills.py", "state.py", "status.py",
    "statusline.py", "tidy.py", "tracker.py", "trycard.py", "usage_events.py",
    "watch.py", "watch_art.py", "watch_cells.py", "watch_data.py", "watch_layout.py",
    "watch_mouse.py", "watch_open.py", "watch_terminal.py",
)

# Frozen from _parser()'s commands.add_parser(...) calls, including doctor.
CLI_COMMANDS = (
    "design", "adopt", "bench", "doctor", "init", "statusline", "profile", "order",
    "lanes", "skills", "gate", "orch", "plan", "tidy", "brief", "test", "notify",
    "inspect",
)

# Frozen from the original AST: definitions, assignments, and import bindings,
# including private names and both branches of the optional _build import.
CLI_NAMES = (
    "Any", "BUSY_TICK", "Config", "ConfigError", "GIT_SHA", "GhTracker", "GitError",
    "GitRepo", "INBOX_ACTIONS", "InspectRuntimeError", "LaneError", "Path", "PathError",
    "READY", "RUNNING", "RecordingTracker", "RunContext", "SimpleNamespace", "SkillError",
    "SkillManager", "StateStore", "TEST_USAGE", "TickContext", "TickReport", "Tracker",
    "TrackerError", "UTC", "__version__", "_comma_keys", "_comma_numbers", "_inspect",
    "_offer_statusline", "_open_lane_view", "_orch_inbox", "_parser", "_print_status",
    "_refresh_status", "_status", "_test_command", "_tidy", "add_lane", "adopt", "argparse",
    "asdict", "bench", "brief", "capacity", "check_done", "collect_status", "datetime",
    "design", "design_session", "init_repo", "inspect_actions", "inspect_data", "json",
    "kick_tick", "launchd", "live_check", "load_config", "load_global_config", "main",
    "notify", "observe_tree", "orch", "orch_inbox", "orch_queue", "orch_tick_outcome",
    "order", "os", "replace", "run_issue", "run_tests", "set_lane_skills", "set_profile",
    "spawn_notify", "spawn_runner", "start_baseline_process", "status_unknown",
    "subprocess", "summary_values", "sys", "text", "tick", "tidy", "watch", "watch_open",
)

# Regression Done when: run every whole file below as well as this acceptance
# file. All 61 passed on main at 28c8bc2 with Python 3.12.13 / pytest 9.1.1,
# offline with isolated HOME/temp roots and no real signals or model turns.
# Paths are relative to tests/acceptance; pending/red acceptance files are omitted.
CLI_ACCEPTANCE_FILES = (
    "test_adaptive_budgets.py", "test_adopt_baseline.py", "test_adopt_ci.py",
    "test_adopt_survey.py", "test_bench_record.py", "test_bench_report.py",
    "test_board_projects.py", "test_board_status_groups.py", "test_ci_not_started.py",
    "test_claude_only.py", "test_cli_skeleton.py", "test_design_first.py",
    "test_design_session.py", "test_doctor_live.py", "test_gate_open_view.py",
    "test_interactive_flag.py", "test_issue_files.py", "test_lane_fixed_skills.py",
    "test_lane_queue.py", "test_lane_view_d76.py", "test_lanes_skills_keeps_comments.py",
    "test_listing_sanity.py", "test_local_truth.py", "test_local_view.py",
    "test_orch_inbox.py", "test_orch_merge.py", "test_orch_reinstall.py",
    "test_orch_skill.py", "test_orch_verbs.py", "test_order_preread.py",
    "test_order_progress.py", "test_premerge_baseline.py", "test_premerge_slots.py",
    "test_profile_config.py", "test_progress_words.py", "test_publish_board_duplicate.py",
    "test_publish_out_of_scope.py", "test_publish_own_paths.py", "test_question_routing.py",
    "test_resume_after_question.py", "test_runner_local_labels.py", "test_skills_add.py",
    "test_skills_add_partial_pack.py", "test_skills_autosync.py", "test_skills_local.py",
    "test_stall_visibility.py", "test_statusline_wrapper.py", "test_test_slots.py",
    "test_tick_failure_alert.py", "test_tidy_cycle.py", "test_view_compact.py",
    "test_view_names.py", "test_view_notify.py", "test_view_open.py",
    "test_view_open_terminals.py", "test_view_open_triggers.py", "test_view_reload.py",
    "test_view_status.py", "test_view_version.py", "test_view_watch.py",
    "test_watch_never_silent.py",
)


@pytest.fixture(autouse=True)
def no_cli_processes(offline_acceptance, monkeypatch):
    """No processes means no live models, terminals, notifications, or Git I/O."""
    import subprocess

    def forbidden(*args, **kwargs):
        pytest.fail("cli-split acceptance must not launch processes")

    monkeypatch.setattr(subprocess, "Popen", forbidden)


def command_modules():
    assert importlib.util.find_spec("foragent.commands") is not None, (
        "CLI groups need a discoverable foragent.commands package"
    )
    package = importlib.import_module("foragent.commands")
    modules = {
        info.name.rsplit(".", 1)[-1]: importlib.import_module(info.name)
        for info in pkgutil.iter_modules(package.__path__, package.__name__ + ".")
        if not info.ispkg
    }
    missing = sorted(set(CLI_COMMANDS) - modules.keys())
    assert not missing, f"each CLI group needs its own module: {missing}"
    return package, modules


def caller_modules():
    frame = sys._getframe(1)
    modules = set()
    while frame is not None:
        modules.add(frame.f_globals.get("__name__"))
        frame = frame.f_back
    return modules


def command_choices(parser):
    actions = [action for action in parser._actions
               if isinstance(action, argparse._SubParsersAction)]
    assert len(actions) == 1, "fa must expose one set of top-level command groups"
    return actions[0]


def test_cli_split_modules_meet_line_limits():
    from foragent import cli

    missing = [name for name in CLI_NAMES if not hasattr(cli, name)]
    assert not missing, f"foragent.cli lost original bindings: {missing}"


def test_cli_split_discovered_groups_own_their_parsers_and_preserve_help(monkeypatch, capsys):
    from foragent import cli

    package, modules = command_modules()
    owners, discovery = {}, []
    original_add = argparse._SubParsersAction.add_parser

    def add_parser(action, name, **kwargs):
        owners[(id(action), name)] = caller_modules()
        return original_add(action, name, **kwargs)

    monkeypatch.setattr(argparse._SubParsersAction, "add_parser", add_parser)
    previous = sys.getprofile()

    def profile(frame, event, arg):
        # Observe the real function even if imported with `from pkgutil import`.
        if event == "call" and frame.f_code is pkgutil.iter_modules.__code__:
            if tuple(frame.f_locals.get("path") or ()) == tuple(package.__path__):
                discovery.append(True)
        if previous is not None:
            previous(frame, event, arg)

    try:
        sys.setprofile(profile)
        parser = cli._parser()
    finally:
        sys.setprofile(previous)
    assert discovery, "parser construction must discover commands over package.__path__"
    action = command_choices(parser)
    assert set(CLI_COMMANDS) <= action.choices.keys()
    for name in CLI_COMMANDS:
        assert modules[name].__name__ in owners[(id(action), name)], (
            f"foragent.commands.{name} must build its own parser"
        )
    assert cli_exit(cli.main, ["--help"]) == 0
    output = capsys.readouterr()
    listed = {line.split()[0] for line in output.out.splitlines() if line.split()}
    assert set(CLI_COMMANDS) <= listed
    assert output.err == ""


def test_cli_split_original_names_remain_importable_and_parser_patches_work(monkeypatch):
    from foragent import cli

    missing = [name for name in CLI_NAMES if not hasattr(cli, name)]
    assert not missing, f"foragent.cli lost original bindings: {missing}"
    for name in CLI_NAMES:
        with monkeypatch.context() as patch:
            sentinel = object()
            patch.setattr(cli, name, sentinel)
            assert getattr(importlib.import_module("foragent.cli"), name) is sentinel

    command_modules()
    calls = []

    def keys(value):
        calls.append(("keys", value))
        return ["patched-key"]

    def numbers(value):
        calls.append(("numbers", value))
        return [41, 42]

    monkeypatch.setattr(cli, "_comma_keys", keys)
    monkeypatch.setattr(cli, "_comma_numbers", numbers)
    args = cli._parser().parse_args([
        "order", "publish", "plan.json", "--approve-keys", "raw-keys",
        "--approve-numbers", "raw-numbers",
    ])
    assert args.approve_keys == ["patched-key"]
    assert args.approve_numbers == [41, 42]
    assert calls == [("keys", "raw-keys"), ("numbers", "raw-numbers")]


def test_cli_split_appended_modules_appear_in_help_and_dispatch(tmp_path, monkeypatch, capsys):
    from foragent import cli

    package, modules = command_modules()
    # Use a real group's API rather than imposing an unspecified registration hook.
    source = Path(modules["bench"].__file__).read_text(encoding="utf-8")
    baseline = set(command_choices(cli._parser()).choices)
    directory = tmp_path / "extra-commands"
    directory.mkdir()
    before = set(sys.modules)
    calls = []

    def run(args):
        assert "foragent.commands." + args.command in caller_modules()
        calls.append((args.command, args.action, args.scenario_dir, args.json))
        print("discovered " + args.command)
        return 23

    monkeypatch.setattr(cli.bench, "run", run)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(package, "__path__", [*package.__path__, str(directory)])
            added = set()
            for name in ("splitprobez", "splitprobea"):
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Constant) and node.value == "bench":
                        node.value = name
                (directory / f"{name}.py").write_text(ast.unparse(tree) + "\n", encoding="utf-8")
                importlib.invalidate_caches()
                added.add(name)
                assert set(command_choices(cli._parser()).choices) == baseline | added
                assert cli_exit(cli.main, ["--help"]) == 0
                help_output = capsys.readouterr()
                listed = {line.split()[0] for line in help_output.out.splitlines() if line.split()}
                assert added <= listed
                assert help_output.err == ""
                assert cli.main([name, "report", str(tmp_path), "--json"]) == 23
                assert capsys.readouterr().out == f"discovered {name}\n"
            assert calls == [(name, "report", tmp_path, True)
                             for name in ("splitprobez", "splitprobea")]
    finally:
        for name in set(sys.modules) - before:
            if name.startswith(package.__name__ + "."):
                module = sys.modules.pop(name)
                child = name.rsplit(".", 1)[-1]
                if getattr(package, child, None) is module:
                    delattr(package, child)
        importlib.invalidate_caches()


def test_cli_split_gate_handler_honours_facade_patch(tmp_path, monkeypatch):
    from foragent import cli

    command_modules()
    calls = []

    def check_done(snapshot, worktree, acceptance_dir):
        assert "foragent.commands.gate" in caller_modules()
        calls.append((snapshot, worktree, acceptance_dir))
        return 19

    monkeypatch.setattr(cli, "check_done", check_done)
    snapshot, worktree = tmp_path / "snapshot.json", tmp_path / "worktree"
    assert cli.main(["gate", "check-done", "--snapshot", str(snapshot),
                     "--worktree", str(worktree), "--acceptance-dir", "tests/acceptance"]) == 19
    assert calls == [(snapshot, worktree, "tests/acceptance")]


@pytest.mark.parametrize("fails", [False, True], ids=["success", "config-error"])
def test_cli_split_profile_handler_honours_facade_patch(tmp_path, monkeypatch, capsys, fails):
    from foragent import cli

    command_modules()
    calls = []
    fields = {"kind": "daily-app", "stage": "build", "priority": "speed"}

    def set_profile(root, **values):
        assert "foragent.commands.profile" in caller_modules()
        calls.append((root, values))
        if fails:
            raise cli.ConfigError("offline profile failure")
        return {"profile": values}

    monkeypatch.setattr(cli, "set_profile", set_profile)
    argv = ["profile", "set", "--repo-root", str(tmp_path)]
    for key, value in fields.items():
        argv.extend([f"--{key}", value])
    assert cli.main(argv) == (1 if fails else 0)
    assert calls == [(tmp_path, fields)]
    output = capsys.readouterr()
    if fails:
        assert output.out == ""
        assert output.err == "foragent: offline profile failure\n"
    else:
        assert json.loads(output.out) == {"profile": fields}
        assert output.err == ""


def test_cli_split_test_handler_keeps_facade_patch_and_raw_pytest_args(tmp_path, monkeypatch):
    from foragent import cli

    command_modules()
    config, calls = object(), []
    monkeypatch.setattr(cli, "load_config", lambda root: config)

    def run_tests(root, settings, argv):
        assert "foragent.commands.test" in caller_modules()
        calls.append((root, settings, argv))
        return 17

    monkeypatch.setattr(cli, "run_tests", run_tests)
    argv = ["-q", "-k", "gate or orch", "tests/test_cli.py", "--maxfail=1"]
    assert cli.main(["test", "--repo-root", str(tmp_path), "--", *argv]) == 17
    assert calls == [(tmp_path, config, argv)]


def test_cli_split_statusline_handler_stays_repository_free(monkeypatch):
    from foragent import cli, statusline

    command_modules()
    calls = []

    def run():
        assert "foragent.commands.statusline" in caller_modules()
        calls.append("statusline")
        return 13

    def repository_access(*args, **kwargs):
        pytest.fail("statusline must run without repository setup")

    monkeypatch.setattr(statusline, "run", run)
    monkeypatch.setattr(cli, "load_config", repository_access)
    monkeypatch.setattr(cli, "GitRepo", repository_access)
    monkeypatch.setattr(sys, "argv", ["fa", "statusline"])
    assert cli.main() == 13
    assert calls == ["statusline"]


def test_cli_split_init_handler_keeps_facade_helper_patches(tmp_path, monkeypatch, capsys):
    from foragent import cli

    command_modules()
    calls = []

    def init_repo(root, **kwargs):
        assert "foragent.commands.init" in caller_modules()
        calls.append(("init", root, kwargs["no_push"], kwargs["no_launchd"]))
        return SimpleNamespace(normalized_root=root, summary=lambda: "offline init")

    monkeypatch.setattr(cli, "init_repo", init_repo)
    monkeypatch.setattr(cli, "_offer_statusline", lambda answer: calls.append(("statusline", answer)))
    monkeypatch.setattr(cli, "_open_lane_view", lambda root: calls.append(("view", root)))
    assert cli.main(["init", "--repo-root", str(tmp_path), "--no-push", "--no-launchd",
                     "--no-board", "--statusline", "no"]) == 0
    assert calls == [("init", tmp_path, True, True), ("statusline", "no"), ("view", tmp_path)]
    assert capsys.readouterr().out == "offline init\n"
