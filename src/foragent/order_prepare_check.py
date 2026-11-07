"""Done-when commands run once with host permissions in a throwaway copy, for the reviewer (#2129).

The isolated reviewer cannot run a project's real test command: its sandbox has no network and
no writable place outside its tree, so tools such as uv (a user cache) or `fa test` (the machine's
test-slot lock) stop before pytest collects anything. Prepare therefore runs each written Done when
command itself, as `fa order red` and the lanes already do on this host: in a new copy of the
candidates' base with exactly the reviewer's files and candidates, never in the caller's checkout.
The reviewer gets the measured command, exit code and output; its own seat stays isolated.

Each copy is checked like the reviewer's tree before it is removed; a run that changed the copy
voids the measurement. The copy lives at ``review-N/check`` and is removed afterwards; the combined output is kept in
``review-N/check.log`` for a person to read later.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
from pathlib import Path

from foragent import capacity, order_scratch, processes

OUTPUT_HEAD = 10_000  # reserve space for tracebacks before warning noise
OUTPUT_TAIL = 20_000  # the end holds pytest's summary and the runner's last line

# Pytest owns node ids and outcomes. Keep its reports outside the measured tree,
# with one file per session so a shell command can invoke pytest more than once.
_NODE_REPORTER = '''
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest


class _Recorder:
    """Facts of one measured session, registered on that session's own plugin manager only.

    A pytest started inside the session, in a subprocess or in-process through pytest.main,
    has its own plugin manager and finds no reports folder, so it neither records nor feeds
    these facts. The folder returns when the session ends, so a later top-level session in
    the same command records again.
    """

    def __init__(self, session, reports):
        self.session, self.reports = session, reports
        self.facts = {"collected": None, "reports": [], "collection_error": False}

    def _nodeid(self, nodeid, path=None):
        name, separator, suffix = nodeid.partition("::")
        source = Path(path) if path is not None else Path(self.session.config.rootpath) / name
        try:
            relative = source.resolve().relative_to(Path(os.environ["FORAGENT_RED_TREE"]).resolve())
        except ValueError:
            self.facts["collection_error"] = True
            return nodeid
        return relative.as_posix() + separator + suffix

    def pytest_collection_finish(self, session):
        self.facts["collected"] = [self._nodeid(item.nodeid, item.path) for item in session.items]

    def pytest_collectreport(self, report):
        if report.failed:
            self.facts["collection_error"] = True

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node, ids):
        ids = [self._nodeid(nodeid) for nodeid in ids]
        if self.facts["collected"] is None:
            self.facts["collected"] = list(ids)
        elif set(self.facts["collected"]) != set(ids):
            self.facts["collection_error"] = True

    def pytest_runtest_logreport(self, report):
        self.facts["reports"].append({
            "nodeid": self._nodeid(report.nodeid), "when": report.when, "outcome": report.outcome,
            "red": getattr(report, "foragent_red", False), "xfail": hasattr(report, "wasxfail"),
        })

    def pytest_sessionfinish(self, session, exitstatus):
        if self.reports is None:
            return  # Workers execute subsets; only the controller has all outcomes.
        try:
            self.facts.update(exit_code=int(exitstatus))
            path = Path(self.reports) / (uuid4().hex + ".json")
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.facts), encoding="utf-8")
            temporary.replace(path)
        finally:
            os.environ["FORAGENT_RED_REPORTS"] = self.reports


def pytest_sessionstart(session):
    if hasattr(session.config, "workerinput"):
        reports = None  # An xdist worker reports through its controller and writes nothing.
    else:
        # Only one session records at a time: a pytest this one starts finds no folder.
        reports = os.environ.pop("FORAGENT_RED_REPORTS", None)
        if reports is None:
            return
    session.config.pluginmanager.register(_Recorder(session, reports), "foragent-red-recorder")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    report = (yield).get_result()
    # xdist serializes report attributes and delivers them to the controller.
    report.foragent_red = (report.when == "call" and report.failed and call.excinfo is not None
                           and not hasattr(report, "wasxfail"))
'''


def _node_facts(directory: Path) -> dict:
    """Keep every failure and nonpassing outcome across sessions; only complete runs prove red."""
    unavailable = dict.fromkeys(("failing_node_ids", "collected_node_ids", "nonred_node_ids",
                                "nonpassing_node_ids"))
    collected, failing, nonred, nonpassing = set(), set(), set(), set()
    complete = True
    paths = list(directory.glob("*.json"))
    if not paths:
        return unavailable
    try:
        for path in paths:
            session = json.loads(path.read_text(encoding="utf-8"))
            selected = session["collected"]
            reports = session["reports"]
            nonpassing.update(report["nodeid"] for report in reports
                              if report["outcome"] != "passed" or report.get("xfail", False))
            if (session["exit_code"] not in (0, 1) or session["collection_error"]
                    or not selected or any(report["when"] in {"setup", "teardown"}
                                           and report["outcome"] == "failed" for report in reports)):
                complete = False
            calls = {report["nodeid"]: report for report in reports if report["when"] == "call"}
            # Early stopping, skips in setup, or a crash do not prove all selected tests ran.
            if set(calls) != set(selected):
                complete = False
            collected.update(selected)
            failing.update(report["nodeid"] for report in reports if report["when"] == "call" and report["red"])
            nonred.update(report["nodeid"] for report in reports if report["when"] == "call" and not report["red"])
    except (OSError, ValueError, TypeError, KeyError):
        return unavailable
    if not complete:
        return {**unavailable, "nonpassing_node_ids": sorted(nonpassing)}
    return {"failing_node_ids": sorted(failing), "collected_node_ids": sorted(collected),
            "nonred_node_ids": sorted(nonred), "nonpassing_node_ids": sorted(nonpassing)}


def _output_excerpt(output: str) -> str:
    """Keep pytest's first failure details and final summary within a fixed budget."""
    if len(output) <= OUTPUT_TAIL:
        return output
    failure = re.search(r"^=+ (?:FAILURES|ERRORS) =+", output, re.MULTILINE)
    if failure is None:
        return output[-OUTPUT_TAIL:]
    head_end = failure.start() + OUTPUT_HEAD
    tail_start = len(output) - OUTPUT_TAIL
    if head_end >= tail_start:
        return output[failure.start():]
    omitted = tail_start - head_end
    return (output[failure.start():head_end] + f"\n... [{omitted} characters omitted] ...\n"
            + output[tail_start:])


def run(git, base: str, context, place, verify, commands: list[str], directory: Path,
        timeout_s: int, *, collect_node_ids: bool = False) -> list[dict]:
    """``{"command", "exit_code", "timed_out", "output", "changed"}`` per command.

    Each command gets its own new copy under ``directory``, so nothing one command leaves behind
    reaches the next. ``place(tree)`` adds the candidates exactly as the reviewer's tree has them;
    ``verify(tree, manifest, before)`` is the reviewer tree's own integrity check, run on the copy
    after the command: a run that changed, removed or touched a candidate or any other file of base
    measured something else, so its ``changed`` paths void it. The prepare caller already holds a
    test slot, so ``capacity.HELD_ENV`` keeps an inner `fa test` from waiting for a second one.
    ``collect_node_ids`` additionally records pytest's exact collection, call failures and
    ``nonpassing_node_ids`` (skips, xfails, xpasses and failures in any phase or session) for
    off-owner publication checks, without interpreting terminal output or starting a reviewer.
    """
    copy, log = directory / "check", directory / "check.log"
    if collect_node_ids:
        plugin = directory / "pytest-reporter"
        plugin.mkdir()
        # Pytest discovers this parent conftest by path, even if a command assigns
        # PYTHONPATH or uses its own interpreter. Keep instrumentation outside
        # the measured tree and preserve the project's own pytest configuration.
        (plugin / "conftest.py").write_text(_NODE_REPORTER, encoding="utf-8")
        copy = plugin / "check"
    results = []
    for number, command in enumerate(commands):
        environment = {**capacity.HELD_ENV, "UV_PROJECT": None, "UV_WORKING_DIRECTORY": None,
                       "UV_WORKING_DIR": None, "UV_PROJECT_ENVIRONMENT": None, "UV_NO_SYNC": None,
                       "PYTHONPATH": None}
        if collect_node_ids:
            reports = directory / f"pytest-reports-{number}"
            reports.mkdir()
            environment.update(PYTEST_ADDOPTS=(os.environ.get("PYTEST_ADDOPTS", "")
                                              + " --confcutdir=" + shlex.quote(str(plugin.resolve()))),
                               FORAGENT_RED_REPORTS=str(reports.resolve()), FORAGENT_RED_TREE=str(copy.resolve()),
                               PYTHONDONTWRITEBYTECODE="1")
        try:
            manifest = order_scratch.build(git, base, copy, context)
            place(copy)
            before = order_scratch.stamps(copy, order_scratch._entries(copy))  # candidates too
            with log.open("ab") as handle:
                handle.write(f"$ {command}\n".encode("utf-8"))
                start = handle.tell()
            code, timed_out, _ = processes.run_logged(
                order_scratch.command_argv(copy, command), cwd=copy, timeout_s=timeout_s, log_path=log,
                env=environment)
            changed = verify(copy, manifest, before)
        finally:
            shutil.rmtree(copy, ignore_errors=True)
        with log.open("rb") as handle:
            handle.seek(start)
            output = handle.read().decode("utf-8", "replace")
        results.append({"command": command, "exit_code": code, "timed_out": timed_out,
                        "output": _output_excerpt(output), "changed": changed,
                        **(_node_facts(reports) if collect_node_ids else {})})
    return results


def changed(results: list[dict]) -> list[str]:
    """Every path a host run changed in its copy; any of them voids the measurement."""
    return sorted({name for run_ in results for name in run_["changed"]})


def section(results: list[dict]) -> str:
    """The prompt section that hands the measured results to the reviewer."""
    if not results:
        return "(no command was run)\n"
    parts = []
    for run_ in results:
        status = "timed out" if run_["timed_out"] else f"exit code {run_['exit_code']}"
        parts.append(f"### `{run_['command']}` ({status})\n```\n{run_['output']}\n```\n")
    return "".join(parts)


def contradicted(red: list[dict], results: list[dict]) -> list[str]:
    """Commands whose reported exit code differs from the one prepare measured."""
    measured = {run_["command"]: run_["exit_code"] for run_ in results if not run_["timed_out"]}
    return [run_.get("command") for run_ in red
            if run_.get("command") in measured and run_.get("exit_code") != measured[run_["command"]]]
