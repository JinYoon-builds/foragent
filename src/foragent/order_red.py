"""`fa order red PLAN.json`: whether a lane's test draft fails on the fetched base for a real reason.

The draft's acceptance files run in a scratch copy of the base branch as fetched, never the caller's
checkout, through the project's own pytest command. A small plugin in the copy writes one fact per
collection and test report; the verdict reads those facts, not pytest's output text. Red means every
named file collects at least one test and every test fails in its call by an exception that is not a
missing module. Nothing in the checkout, its index, branches or GitHub changes and no model starts.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from foragent import order, order_collect
from foragent.config import load_config
from foragent.git import GitError, GitRepo
from foragent.i18n import text
from foragent.issue import acceptance_paths

# Slot admission and the run share this budget; running out means no verdict, not "not red".
LIMIT_S = 600.0
_ERROR_CHARS = 600
_REPORT = "red.jsonl"
# Registered as a plugin, not a conftest hook, so it sees reports for files outside its folder.
_REPORTER = """

import json as _json


def _missing(excinfo):
    seen, error = set(), excinfo.value if excinfo is not None else None
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, ModuleNotFoundError):
            return error.name or str(error)
        error = error.__cause__ or error.__context__
    return None


class RedReport:
    def __init__(self):
        self.out = open(os.path.join(os.path.dirname(__file__), "red.jsonl"), "a", encoding="utf-8")
        self.calls = {}

    def write(self, row):
        self.out.write(_json.dumps(row) + "\\n")
        self.out.flush()

    def pytest_collection_modifyitems(self, items):
        counts = {}
        for item in items:
            name = item.nodeid.split("::")[0]
            counts[name] = counts.get(name, 0) + 1
        self.write({"phase": "items", "counts": counts})

    def pytest_collectreport(self, report):
        if report.failed:
            self.write({"phase": "collect", "nodeid": report.nodeid, "outcome": "failed",
                        "error": str(report.longrepr)})

    def pytest_runtest_makereport(self, item, call):
        if call.excinfo is not None:
            self.calls[(item.nodeid, call.when)] = (call.excinfo.typename, _missing(call.excinfo))

    def pytest_runtest_logreport(self, report):
        exception, missing = self.calls.get((report.nodeid, report.when), (None, None))
        if report.failed and exception is None and getattr(report, "longrepr", None) is not None:
            exception = "failure"
        self.write({"phase": report.when, "nodeid": report.nodeid, "outcome": report.outcome,
                    "xfail": hasattr(report, "wasxfail"), "exception": exception, "missing": missing,
                    "error": str(report.longrepr) if report.failed else ""})


_published_configure = pytest_configure


def pytest_configure(config):
    _published_configure(config)
    config.pluginmanager.register(RedReport(), "foragent-red-report")
"""


def _language(config) -> str:
    return (getattr(config, "language", None) or "en").lower().split("-")[0]


def draft_files(plan: dict, config) -> tuple[dict[str, str | bytes], list[str]]:
    """(contents the plan carries, every acceptance file to run, sorted)."""
    carried: dict[str, str | bytes] = {}
    names: set[str] = set()
    for item in [*plan.get("issues", []), *plan.get("edits", [])]:
        files = item.get("acceptance_files") or {}
        carried.update(files)
        names.update(files)
        names.update(acceptance_paths(item.get("done_when", []), config.acceptance_dir))
    return carried, sorted(names)


def _rows(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [json.loads(line) for line in lines if line.strip()]


def _short(error: str, tree: Path) -> str:
    lines = [line for line in error.replace(f"{tree}/", "").splitlines() if line.strip()]
    return " | ".join(lines[-3:])[-_ERROR_CHARS:]


def verdict(rows: list[dict], names: list[str], tree: Path, language: str) -> list[str]:
    """One line per item that is not red; empty when the draft is red."""
    say = lambda key, **values: text(key, language, **values)  # noqa: E731
    problems: list[str] = []
    counts: dict[str, int] = {}
    for row in rows:
        if row["phase"] == "items":
            counts = row["counts"]
    broken = {row["nodeid"] for row in rows if row["phase"] == "collect"}
    for row in rows:
        if row["phase"] == "collect":
            problems.append(say("order_red_collect_error", file=row["nodeid"] or "-",
                                error=_short(row["error"], tree)))
    for name in names:
        if not counts.get(name) and name not in broken:
            problems.append(say("order_red_empty", file=name))
    tests: dict[str, dict[str, dict]] = {}
    for row in rows:
        if row["phase"] in {"setup", "call", "teardown"}:
            tests.setdefault(row["nodeid"], {})[row["phase"]] = row
    for nodeid, phases in tests.items():
        setup, call, teardown = phases.get("setup"), phases.get("call"), phases.get("teardown")
        if setup and setup["outcome"] == "failed":
            problems.append(say("order_red_setup_error", test=nodeid, error=_short(setup["error"], tree)))
        elif setup and setup["outcome"] == "skipped" or call and call["outcome"] == "skipped":
            problems.append(say("order_red_skipped", test=nodeid))
        elif call is None:
            problems.append(say("order_red_not_run", test=nodeid))
        elif call["xfail"]:
            problems.append(say("order_red_xfail", test=nodeid))
        elif call["outcome"] == "passed":
            problems.append(say("order_red_passes", test=nodeid))
        elif call["missing"]:
            problems.append(say("order_red_missing", test=nodeid, module=call["missing"]))
        elif not call["exception"]:
            problems.append(say("order_red_no_exception", test=nodeid))
        if teardown and teardown["outcome"] == "failed":
            problems.append(say("order_red_teardown_error", test=nodeid, error=_short(teardown["error"], tree)))
    return problems


def check(plan: dict, root: Path, config, git: GitRepo | None = None) -> dict:
    """{"red", "base", "files", "problems"}; raises PlanError when no verdict could be reached."""
    language = _language(config)
    carried, names = draft_files(plan, config)
    if not names:
        raise order.PlanError(text("order_red_no_files", language))

    def unavailable(cause: str, command: str | None) -> order.PlanError:
        return order.PlanError(text("order_red_unavailable", language, files=", ".join(names),
                                    command=command or text("order_collect_no_command_name", language),
                                    cause=cause))

    git = git or GitRepo(root)
    try:
        base = order_collect._current_base(git, config.base_branch)
    except (GitError, OSError, subprocess.SubprocessError) as exc:
        cause = text("order_collect_no_base", language, branch=config.base_branch, error=" ".join(str(exc).split()))
        raise unavailable(cause, config.test_command) from exc
    work = None
    try:
        work = Path(tempfile.mkdtemp(prefix="foragent-red-"))
        code, tree, log, attempted = order_collect.run_copy(
            git, base, carried, names, work, config, language, time.monotonic() + LIMIT_S,
            before="-p no:cacheprovider", after="", conftest=order_collect._COPY_CONFTEST + _REPORTER,
            timeout=text("order_red_timeout", language, seconds=int(LIMIT_S)),
            slot_timeout=text("order_red_slot_timeout", language))
        rows = _rows(tree / ".foragent-collect" / _REPORT)
        if code not in {0, 1, 2, 5} or not any(row["phase"] == "items" for row in rows):
            raise order_collect._Unavailable(
                text("order_collect_exit", language, code=code, output=order_collect._output(log, tree)), attempted)
        problems = verdict(rows, names, tree, language)
    except order_collect._Unavailable as exc:
        raise unavailable(exc.args[0], exc.args[1]) from exc
    except Exception as exc:  # Whatever stopped the check, it did not establish a verdict.
        raise unavailable(f"{type(exc).__name__}: {' '.join(str(exc).split())}", config.test_command) from exc
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
    return {"red": not problems, "base": base, "files": names, "problems": problems}


def run(args, root: Path) -> int:
    """Exit 0 when red, 1 when not red, 2 when the check could not finish."""
    config = load_config(root)
    language = _language(config)
    try:
        result = check(order.read_plan(args.plan), root, config)
    except (order.PlanError, OSError, ValueError) as exc:
        if args.json:
            print(json.dumps({"red": None, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"foragent: {exc}")
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    elif result["red"]:
        print(text("order_red_yes", language, base=result["base"][:12], files=", ".join(result["files"])))
    else:
        print(text("order_red_no", language, base=result["base"][:12], count=len(result["problems"])))
        for problem in result["problems"]:
            print(f"  - {problem}")
    return 0 if result["red"] else 1
