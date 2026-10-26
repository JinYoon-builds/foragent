"""Command-line entry points for the issue gate.

Each top-level group lives in ``foragent.commands.<name>``; this module builds the
parser from the discovered groups and dispatches to them. The names below stay
importable here, and the groups read them from here, so patches keep working.
"""

import argparse
from dataclasses import asdict, replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import yaml

from foragent import __version__, adopt, bench, brief, capacity, design, design_session, inspect_actions, inspect_data, launchd, machine_budget, notify, orch, orch_facts, orch_inbox, orch_queue, orch_tick_outcome, order, star, tidy, update_check, watch, watch_open
from foragent.config import Config, ConfigError, load_config, load_global_config, set_profile
from foragent.git import GitError, GitRepo
from foragent.init import init_repo
from foragent.i18n import text
from foragent.kick import kick_tick
from foragent.labels import READY, RUNNING
from foragent.lanes import LaneError, add_lane, set_lane_skills
from foragent.paths import PathError
from foragent.skills import SkillError, SkillManager
from foragent.launcher import spawn_notify, spawn_runner
from foragent.inspect_runtime import InspectRuntimeError
from foragent.processes import observe_tree
from foragent.scheduler import BUSY_TICK, TickContext, TickReport, tick
from foragent.runner import RunContext, check_done, live_check, run_issue, run_tests
from foragent.run_baseline import start_baseline_process
from foragent.state import StateStore
from foragent.status import collect as collect_status, summary_values, unknown as status_unknown
from foragent.tracker import GhTracker, RecordingTracker, Tracker, TrackerError
from foragent import commands
from foragent.commands import _open_lane_view
from foragent.commands.gate import _print_machine_budget, _print_status, _refresh_status, _status
from foragent.commands.init import _offer_statusline
from foragent.commands.inspect import _inspect
from foragent.commands.order import _comma_keys, _comma_numbers
from foragent.commands.orch import INBOX_ACTIONS, _orch_inbox
from foragent.commands.statusline import _uninstall_statusline
from foragent.commands.test import TEST_USAGE, _test_command
from foragent.commands.tidy import _tidy

try:
    from foragent._build import GIT_SHA
except ImportError:
    GIT_SHA = "unknown"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fa")
    parser.add_argument("--version", action="version", version=f"foragent {__version__} ({GIT_SHA})")
    groups = parser.add_subparsers(dest="command", required=True)
    for group in commands.discover():
        group.add(groups)
    return parser


# setpriority(2) "which" for a process's macOS darwin-background state.
PRIO_DARWIN_PROCESS = 4


def _leave_background() -> None:
    """Gate work never inherits background QoS from whoever started it.

    A background process (launchd ProcessType Background, or a tick kicked by one)
    runs on throttled efficiency cores, and its children inherit that state.
    """
    if sys.platform != "darwin":
        return
    try:
        os.setpriority(PRIO_DARWIN_PROCESS, 0, 0)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["test"]:
        return _test_command(argv[1:])
    if argv == ["statusline"]:
        # Claude Code runs this on every refresh; keep it repository-free and byte-exact.
        return commands.module("statusline").run(None)
    args = _parser().parse_args(argv)
    if args.command == "gate":
        _leave_background()
    group = commands.module(args.command)
    if args.command == "statusline":
        return group.run(args)
    try:
        return group.run(args)
    except (ConfigError, TrackerError, GitError, launchd.LaunchdError, OSError, design_session.DesignError,
            inspect_actions.InspectError, InspectRuntimeError, subprocess.SubprocessError,
            order.PlanError, orch.OrchError, orch_inbox.InboxError, LaneError, SkillError, PathError, json.JSONDecodeError) as exc:
        print(f"foragent: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
