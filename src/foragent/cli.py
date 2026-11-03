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
import shutil
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from typing import Any

try:
    # The gate's locks need POSIX fcntl; only native Windows lacks it (WSL is Linux).
    # Stop here, before launchd/git and the rest import it, with one clear line.
    import fcntl  # noqa: F401
except ImportError:
    print("foragent: native Windows is not supported yet; use macOS, or Linux/WSL.", file=sys.stderr)
    raise SystemExit(1) from None

import yaml

from foragent import __version__, adopt, bench, brief, brief_report, capacity, design, design_session, gates, gh, inspect_actions, inspect_data, launchd, machine_budget, notify, orch, orch_dream, orch_facts, orch_inbox, orch_queue, orch_tick_outcome, order, star, tidy, update_check, watch, watch_open
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
from foragent.commands.gate import _print_gates, _print_machine_budget, _print_status, _refresh_status, _status
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


# The commands a person types; every other discovered command is for Foragent's own sessions.
PERSON = ("init", "doctor", "inspect", "gate")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fa", usage="fa [-h] [--version] <command> ...",
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"foragent {__version__} ({GIT_SHA})")
    groups = parser.add_subparsers(dest="command", required=True, metavar="<command>", prog="fa",
                                   help=argparse.SUPPRESS)
    for group in commands.discover():
        group.add(groups)
    parser.description = _command_list(groups)
    return parser


def _command_list(groups) -> str:
    """The help's command block: where to start, then person and internal commands."""
    helps = {action.dest: action.help or "" for action in groups._choices_actions}
    names = list(groups.choices)
    width = max(map(len, names)) + 4
    wrap = max(shutil.get_terminal_size().columns - 2 - width, 20)

    def block(title, chosen):
        lines = [title]
        for name in chosen:
            body = textwrap.wrap(helps.get(name, ""), wrap) or [""]
            lines += [f"  {name:<{width - 2}}{body[0]}".rstrip(), *(" " * width + line for line in body[1:])]
        return lines

    person = [name for name in PERSON if name in groups.choices]
    internal = [name for name in names if name not in PERSON]
    return "\n".join(["Start with fa init, then /fa.", "", *block("Person commands:", person), "",
                      *block("Internal commands:", internal)])


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


# `fa order` actions that publish or approve; a displaced PM session refuses them (#1484).
PM_ONLY_ORDER_ACTIONS = ("publish", "flush", "edit")


def _pm_project(cwd: Path):
    """(config, store, root) for the project around ``cwd``, else None; reads no child process."""
    from foragent import presence

    project = presence._project(cwd)
    if project is None or project[1] is None:
        return None
    return project[0], project[1], presence.project_root(cwd)


def _enter_pm() -> int:
    """Bare ``fa``: make this Claude session the person's PM for this project (#1484).

    The latest explicit entrance wins; the session it replaces is told on its next
    prompt and refuses publication and approval. A shell without a Claude session
    (no FORAGENT_SESSION_ID) gets one line and claims nothing.
    """
    from foragent import status_pm

    try:
        project = _pm_project(Path.cwd())
    except (ConfigError, OSError):
        project = None
    language = project[0].language if project else "en"
    session = status_pm.session_from_env()
    if session is None:
        print(text("pm_seat_no_session", language))
        return 0
    if project is None:
        print(text("pm_seat_no_project", language), file=sys.stderr)
        return 1
    config, store, root = project
    from foragent import presence

    repo = presence._repo(config, root)
    try:
        result = status_pm.claim(store, session, datetime.now(UTC), root)
    except status_pm.SeatUnreadable as exc:
        print(text("pm_seat_unreadable", language, reason=str(exc)), file=sys.stderr)
        return 1
    previous = result["previous"]
    if previous is not None:
        print(text("pm_seat_taken_from", language, repo=repo, session=session,
                   previous=status_pm.short(previous["session_id"])))
    else:
        print(text("pm_seat_kept" if result["kept"] else "pm_seat_taken", language, repo=repo, session=session))
    return 0


def _displaced_pm(root: Path) -> str | None:
    """The refusal line when this session lost the person's PM seat for ``root``, else None."""
    from foragent import status_pm

    try:
        project = _pm_project(root)
    except (ConfigError, OSError):
        return None  # The command itself reports an unreadable project.
    if project is None:
        return None
    config, store, _root = project
    try:
        status_pm.require_not_displaced(store, status_pm.session_from_env())
    except status_pm.SeatMoved as moved:
        return text("pm_seat_moved", config.language, holder=status_pm.short(moved.holder["session_id"]))
    except status_pm.SeatUnreadable as exc:
        return text("pm_seat_unreadable", config.language, reason=str(exc))
    return None


# Commands only a person (or the orchestrator) may run, keyed by (command, action); the value
# is the option that makes the command person-only, or None when every form is (#1736).
PERSON_ONLY = {
    ("orch", "resolve"): "verdict",
    ("orch", "merge"): "approved_by",
    ("skills", "add"): "approve",
    ("settings", "set"): None,
    ("init", None): "fa_allow",
    ("role", "add"): None,
    ("role", "set"): None,
    ("role", "remove"): None,
    ("lanes", "skills"): None,
}


def _project_language(root: Path | None) -> str:
    """The language the project around ``root`` (else the current folder) speaks, read only.

    The nearest ``.foragent.yml`` decides; when it names none, or no project folder can
    be found, the person's own config does, as load_config would overlay it. English otherwise.
    """
    from foragent import settings

    try:
        start = Path(root or Path.cwd()).resolve()
        folder = next((folder for folder in (start, *start.parents) if (folder / ".foragent.yml").is_file()), start)
    except OSError:
        folder = Path(os.devnull)  # No folder to look in: only the personal config is left.
    return settings.language(folder)


def _launched_refusal(args) -> str | None:
    """The refusal line when a session Foragent launched runs a person-only command, else None.

    Executors mark every session they start (``executors.LAUNCHED_ENV``); a person's
    terminal and the PM session have no FORAGENT_ROLE, the orchestrator keeps its own.
    """
    role = os.environ.get("FORAGENT_ROLE")
    if role is None or role == "orchestrator":
        return None
    action = getattr(args, "action", None)
    key = (args.command, action)
    if key not in PERSON_ONLY:
        return None
    option = PERSON_ONLY[key]
    if option is not None and getattr(args, option, None) in (None, False):
        return None
    command = " ".join(part for part in (args.command, action) if part)
    if option is not None:
        command += " --" + option.replace("_", "-")
    return text("launched_person_only", _project_language(getattr(args, "repo_root", None)), command=command)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv:
        return _enter_pm()
    if argv[:1] == ["test"]:
        return _test_command(argv[1:])
    if argv == ["statusline"]:
        # Claude Code runs this on every refresh; keep it repository-free and byte-exact.
        return commands.module("statusline").run(None)
    args = _parser().parse_args(argv)
    refused = _launched_refusal(args)
    if refused:
        print(f"foragent: {refused}", file=sys.stderr)
        return 1
    if args.command == "gate":
        _leave_background()
    if (args.command == "order" and getattr(args, "action", None) in PM_ONLY_ORDER_ACTIONS
            and not getattr(args, "dry_run", False)):
        refused = _displaced_pm(getattr(args, "repo_root", None) or Path.cwd())
        if refused:
            print(f"foragent: {refused}", file=sys.stderr)
            return 1
    group = commands.module(args.command)
    if args.command == "statusline":
        return group.run(args)
    try:
        return group.run(args)
    except order.DoneWhenExecutableError as exc:
        for line in exc.lines:
            print(f"foragent: {line}", file=sys.stderr)
        return 1
    except (ConfigError, TrackerError, GitError, launchd.LaunchdError, OSError, design_session.DesignError,
            inspect_actions.InspectError, InspectRuntimeError, subprocess.SubprocessError,
            order.PlanError, orch.OrchError, orch_inbox.InboxError, LaneError, SkillError, PathError, json.JSONDecodeError) as exc:
        print(f"foragent: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
