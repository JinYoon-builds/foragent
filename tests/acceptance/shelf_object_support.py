"""Shared helpers for the shelf-object acceptance tests (issue shelf-object).

Public contract these helpers read (the lane's DECIDE choices for this issue):

- The object lives in ``foragent.shelf`` (module names ``foragent.shelf*``). ``shelf.Place(name=...,
  purpose=..., reader=..., why=..., trigger=..., channel=..., alarm={"count", "age"},
  capacity={"in", "out"}, removal=..., person_load={"cap", "form"})`` creates a place;
  ``shelf.places()`` lists the places the gate asks each tick.
- Phase-1 places: ``run-records`` (<state>/runs/*.json), ``logs`` (every file under <state>/logs/),
  ``held-runs`` (held or blocked runs, and closed issues whose run still holds a slot) and
  ``drafts`` (<state>/drafts/<n>/).
- ``fa brief --json`` carries ``shelves``: place name -> ``{"count", "in_24h", "out_24h",
  "oldest_minutes"}`` (whole minutes, None when empty) and ``pm_alarms``: the open PM alarms.
- An alarm is one orchestrator request box record ``from: gate``, ``kind: shelf-alarm``,
  ``place: <name>`` and ``to: orchestrator`` (the owner of a gate-read place) or ``to: pm``.
  An episode's end is one orch-events record ``kind: shelf-cleared`` with ``place: <name>``.
- Over a limit means more items than the count limit, or an oldest item older than the age limit.
"""

from __future__ import annotations

from datetime import timedelta
import importlib.util
import json
import os
import re
import sys
import threading

from test_skills_autosync import cli_exit


PLACES = ("run-records", "logs", "held-runs", "drafts")
HELD_AGE = timedelta(minutes=15)
HELD_COUNT = 3
LOGS_AGE = timedelta(days=30)
GOAL_BODY = ("<!-- foragent:v1 -->\n## Goal\nDo the work.\n## Out of scope\nOther work.\n"
             "## Done when\n```sh\ntrue\n```\n")


# ---------------------------------------------------------------- the object


def shelf_module():
    """The accumulation object; its absence is an assertion failure, not a collection error."""
    spec = importlib.util.find_spec("foragent.shelf")
    assert spec is not None, "There is no accumulation object (foragent.shelf) to create places through"
    import foragent.shelf as shelf

    return shelf


def part(value, key):
    """A sub-field of a place field, given as a mapping or as an attribute."""
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


# ---------------------------------------------------------------- CLI views


def cli(case, capsys, *argv):
    capsys.readouterr()
    code = cli_exit(case.main, [*argv, "--repo-root", str(case.root)])
    output = capsys.readouterr()
    assert code == 0, f"fa {' '.join(argv)} failed: {output.err}"
    return output.out


def brief(case, capsys):
    shown = json.loads(cli(case, capsys, "brief", "--json"))
    assert "shelves" in shown, f"fa brief --json shows no accumulation places: {sorted(shown)}"
    return shown


def shelf_row(shown, name):
    shelves = shown["shelves"]
    row = shelves.get(name) if isinstance(shelves, dict) else next(
        (item for item in shelves if item.get("name") == name), None)
    assert row is not None, f"fa brief --json does not show the {name} place: {shelves}"
    for key in ("count", "in_24h", "out_24h", "oldest_minutes"):
        assert key in row, f"The {name} place shows no {key}: {row}"
    return row


def report_lines(case, capsys):
    return cli(case, capsys, "brief", "--report").splitlines()


def requests(case, capsys):
    capsys.readouterr()
    assert cli_exit(case.main, ["orch", "inbox", "--json", "--repo-root", str(case.root)]) == 0
    return json.loads(capsys.readouterr().out)


def alarms(case, capsys, place, to=None):
    """Every shelf alarm for ``place`` in the request box, open or done."""
    return [row for row in requests(case, capsys)
            if row.get("from") == "gate" and row.get("kind") == "shelf-alarm" and row.get("place") == place
            and (to is None or row.get("to") == to)]


def events(case):
    path = case.store.root / "orch-events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def cleared(case, place):
    return [row for row in events(case) if row.get("kind") == "shelf-cleared" and row.get("place") == place]


def issue_refs(row):
    """Issue numbers an alarm names: ``#N`` in its text, integers in its facts or items."""
    found = {int(number) for number in re.findall(r"#(\d+)", row.get("text") or "")}

    def walk(value):
        if isinstance(value, bool):
            return
        if isinstance(value, int):
            found.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(row.get("facts"))
    walk(row.get("items"))
    return found


# ---------------------------------------------------------------- clock and ticks


def tick_at(case, capsys, at):
    """One real `fa gate tick` at exactly ``at`` (run_tick adds five minutes itself)."""
    from test_tick_failure_alert import run_tick

    assert at > case.clock[0], "Ticks only move forward"
    case.clock[0] = at - timedelta(minutes=5)
    run_tick(case, capsys)
    assert case.clock[0] == at


def quiet_kicks(monkeypatch):
    """`fa orch retry` kicks a tick in the background; these tests run every tick themselves."""
    import foragent.orch

    monkeypatch.setattr(foragent.orch, "kick_tick", lambda root: None)


# ---------------------------------------------------------------- held runs


def hold(case, number, *, reason="retries-exceeded"):
    """An approved issue whose run ended held, written the way run_issue writes it."""
    from foragent.actions import flag
    from foragent.issue import body_hash
    from foragent.reasons import Reason
    from foragent.state import StateStore
    from test_orch_review_handoff import tick_context

    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    case.tracker.add_issue(number, f"Held work {number}", GOAL_BODY, labels=["approved", "running"])
    case.store.save_approval(number, body=GOAL_BODY, body_sha=body_hash(GOAL_BODY), acceptance_blobs={},
                             approved_by="human", approved_at=case.clock[0].isoformat())
    case.store.start_run(number, branch=f"foragent/issue-{number}", worktree=case.store.worktree_path(number))
    flag(tick_context(case), number, Reason(reason), "Checks or review failed after all attempts.")
    case.store.finish_run(number, outcome=reason)


# ---------------------------------------------------------------- write watch (statement 7)


_WATCH = {"current": None}
_LOCAL = threading.local()
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
_EVENTS = frozenset({"open", "os.remove", "os.rename", "os.rmdir", "os.mkdir", "shutil.rmtree",
                     "os.truncate", "os.link", "os.symlink", "shutil.copyfile", "shutil.move"})
OBJECT_MODULE = "foragent.shelf"


class WriteWatch:
    """Records every write the Python process makes under a state folder's runs, logs and drafts.

    The check is by behaviour, not by source text: a CPython audit hook sees the resolved
    destination of each open-for-write, create, rename, delete or tree removal, however the path
    was built (a variable, an absolute path, a temporary file renamed into place). A write is the
    object's when a ``foragent.shelf*`` frame is on the call stack; reads are never counted.
    """

    def __init__(self, root):
        raw = os.path.abspath(os.fspath(root))
        self.roots = {raw, os.path.realpath(raw)}
        self.writes: list[tuple[str, str, str]] = []
        self.violations: list[str] = []

    def area(self, path: str) -> str | None:
        """runs: the run history and run records (runs.jsonl, runs/<key>.json and their temporary
        files); logs: every file under logs/; drafts: everything under drafts/. Creating the three
        folders themselves, lock files and per-run artefact folders under runs/ are not watched."""
        for root in self.roots:
            if path == os.path.join(root, "runs.jsonl") or path.startswith(os.path.join(root, ".runs.jsonl.")):
                return "runs"
            runs = os.path.join(root, "runs") + os.sep
            if path.startswith(runs):
                name = path[len(runs):]
                if os.sep not in name and (name.endswith(".json") or (
                        name.startswith(".") and ".json." in name and name.endswith(".tmp"))):
                    return "runs"
                return None
            for name in ("logs", "drafts"):
                folder = os.path.join(root, name)
                if path.startswith(folder + os.sep):
                    return name
        return None

    def classify(self, value) -> tuple[str, str] | None:
        """(area, path) of a write destination. Both the spelled path and its canonical
        destination (every symlink in its folders resolved) are classified, so an alias inside
        or outside the state folder that points into runs/, logs/ or drafts/ is still seen."""
        if isinstance(value, int) or value is None:
            return None
        try:
            path = os.fsdecode(value)
        except TypeError:
            return None
        path = path if os.path.isabs(path) else os.path.join(os.getcwd(), path)
        lexical = os.path.normpath(path)
        folder, name = os.path.split(lexical)
        real = os.path.join(os.path.realpath(folder), name)
        for candidate in (os.path.realpath(lexical), real, lexical):
            area = self.area(candidate)
            if area:
                return area, candidate
        return None

    def observe(self, event: str, args: tuple) -> None:
        if event == "open":
            flags = args[2] if len(args) > 2 else None
            mode = args[1] if len(args) > 1 else None
            writing = (isinstance(flags, int) and flags & _WRITE_FLAGS) or (
                isinstance(mode, str) and any(letter in mode for letter in "wax+"))
            if not writing:
                return
            targets = [args[0]]
        elif event in {"os.remove", "os.rmdir", "os.mkdir", "shutil.rmtree"}:
            if len(args) > 1 and args[-1] is not None and not os.path.isabs(os.fsdecode(args[0])):
                return  # A name relative to a directory descriptor: its tree removal was seen already.
            targets = [args[0]]
        elif event in {"os.rename", "shutil.move", "shutil.copyfile", "os.link", "os.symlink"}:
            targets = list(args[:2]) if event in {"os.rename", "shutil.move"} else [args[1]]
        else:  # os.truncate
            targets = [args[0]]
        for target in targets:
            found = self.classify(target)
            if not found:
                continue
            area, path = found
            frame, owner, outer = sys._getframe(2), False, None
            while frame is not None:
                module = frame.f_globals.get("__name__", "")
                if module == OBJECT_MODULE or module.startswith(OBJECT_MODULE + "_") \
                        or module.startswith(OBJECT_MODULE + "."):
                    owner = True
                    break
                if outer is None and module.startswith("foragent."):
                    outer = f"{module}.{frame.f_code.co_name}:{frame.f_lineno}"
                frame = frame.f_back
            self.writes.append((area, event, path))
            if not owner:
                self.violations.append(f"{event} {path} from {outer or 'outside foragent'}")

    def areas(self) -> set[str]:
        return {area for area, _event, _path in self.writes}

    def __enter__(self):
        _install()
        assert _WATCH["current"] is None, "One write watch at a time"
        _WATCH["current"] = self
        return self

    def __exit__(self, *exc):
        _WATCH["current"] = None
        return False


def _hook(event, args):
    watch = _WATCH["current"]
    if watch is None or event not in _EVENTS or getattr(_LOCAL, "busy", False):
        return
    _LOCAL.busy = True
    try:
        watch.observe(event, args)
    finally:
        _LOCAL.busy = False


_INSTALLED = []


def _install():
    if not _INSTALLED:
        sys.addaudithook(_hook)  # Process-wide and permanent; idle unless a watch is active.
        _INSTALLED.append(True)
