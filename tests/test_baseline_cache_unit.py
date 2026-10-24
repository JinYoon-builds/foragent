"""Exercise baseline reuse against real pytest subprocesses and local Git history."""

from datetime import timedelta
import json
import shlex
import sys
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from conftest import BASE_TIME, git_command
from foragent.config import Config
from foragent.git import GitRepo
from foragent.run_attempt import AttemptFailure
from foragent.runner import RunContext, _capture_acceptance_baseline
from foragent.state import StateStore
from foragent.tracker import FakeTracker


ACCEPTANCE_DIR = "checks/acceptance/"
FIRST = f"{ACCEPTANCE_DIR}test_first.py"
LAST = f"{ACCEPTANCE_DIR}test_last.py"
PASSING_IDS = (f"{FIRST}::test_first", f"{LAST}::test_last")
COMMAND = f"{shlex.quote(sys.executable)} -m pytest -q"


@pytest.fixture
def baseline_case(git_history, tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    now = [BASE_TIME]
    store = StateStore(tmp_path / "state", now=lambda: now[0])
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1))
    ctx = RunContext(
        repo="owner/repo", repo_root=git_history.clone, tracker=FakeTracker(), store=store,
        config=Config(acceptance_dir=ACCEPTANCE_DIR, max_runtime_minutes=24 * 60),
        git=GitRepo(git_history.clone), now=lambda: now[0],
    )

    def prepare(files):
        git_history.update(files)
        git_command(git_history.clone, "fetch", "-q", "origin")

    prepare({FIRST: "def test_first():\n    pass\n", LAST: "def test_last():\n    pass\n"})
    return SimpleNamespace(
        ctx=ctx, now=now, root=git_history.clone, prepare=prepare,
        cache=tmp_path / "baselines", log=store.log_path(1),
        report=store.root / "runs/issue-1-acceptance-baseline.xml",
        capture=lambda command=COMMAND: _capture_acceptance_baseline(1, ctx, command, git_history.tip),
    )


def gated_test(variable):
    return (
        "import os\nimport pytest\n"
        f"@pytest.mark.skipif(not os.environ.get({variable!r}), reason='environment disabled')\n"
        "def test_last():\n    pass\n"
    )


@pytest.mark.parametrize("budget_exhausted", [False, True])
def test_baseline_caches_all_files_excluded_without_starting_pytest(baseline_case, monkeypatch, budget_exhausted):
    c = baseline_case
    c.ctx.store.save_approval(
        1, body="Approved tests", body_sha="approved",
        acceptance_blobs=c.ctx.git.blob_shas("origin/main", [FIRST, LAST]),
        approved_by="human", approved_at=BASE_TIME.isoformat(),
    )
    if budget_exhausted:
        c.now[0] += timedelta(minutes=c.ctx.config.max_runtime_minutes)
    monkeypatch.setattr("foragent.runner._check", lambda *args, **kwargs: pytest.fail("pytest must not run"))

    first = c.capture("uv run pytest -q")
    assert first is not None and first.node_ids == ()
    assert c.capture("uv run pytest -q") == first
    entry, = c.cache.glob("*.json")
    assert json.loads(entry.read_text()) == {"node_ids": [], "created_at": c.now[0].isoformat()}
    assert not c.report.exists()
    log = c.log.read_text()
    assert log.count("no acceptance test files remain after exclusions") == 1
    assert log.count("Acceptance baseline count=0, cache=hit") == 1


def test_baseline_cache_rebuilds_when_pytest_environment_changes(baseline_case, monkeypatch):
    c = baseline_case
    c.prepare({LAST: gated_test("PYTEST_BASELINE_FEATURE")})
    monkeypatch.delenv("PYTEST_BASELINE_FEATURE", raising=False)
    assert c.capture().node_ids == PASSING_IDS[:1]

    monkeypatch.setenv("PYTEST_BASELINE_FEATURE", "enabled")
    assert c.capture().node_ids == PASSING_IDS
    assert c.capture().node_ids == PASSING_IDS
    assert c.log.read_text().count("cache=hit") == 1
    assert len(list(c.cache.glob("*.json"))) == 2


@pytest.mark.parametrize("dependency", ["uv.lock", "poetry.lock", "pdm.lock", "Pipfile.lock", "pylock.toml", "pyproject.toml"])
def test_baseline_cache_fingerprints_dependency_files(baseline_case, monkeypatch, dependency):
    c = baseline_case
    c.prepare({LAST: gated_test("BASELINE_FEATURE")})
    monkeypatch.delenv("BASELINE_FEATURE", raising=False)
    manifest = c.root / dependency
    manifest.write_text("initial dependencies\n")
    assert c.capture().node_ids == PASSING_IDS[:1]

    manifest.write_text("updated dependencies\n")
    monkeypatch.setenv("BASELINE_FEATURE", "enabled")
    assert c.capture().node_ids == PASSING_IDS
    assert c.capture().node_ids == PASSING_IDS
    assert c.log.read_text().count("cache=hit") == 1


def test_baseline_cache_prefers_lockfiles_over_pyproject(baseline_case):
    c = baseline_case
    (c.root / "uv.lock").write_text("locked dependencies\n")
    (c.root / "pyproject.toml").write_text("initial project\n")
    assert c.capture().node_ids == PASSING_IDS
    (c.root / "pyproject.toml").write_text("updated project\n")
    assert c.capture().node_ids == PASSING_IDS
    assert c.log.read_text().count("cache=hit") == 1


def test_baseline_cache_fingerprints_python_version(baseline_case, monkeypatch):
    c = baseline_case
    assert c.capture().node_ids == PASSING_IDS
    monkeypatch.setattr(sys, "version", sys.version + " changed runtime")
    assert c.capture().node_ids == PASSING_IDS
    assert c.capture().node_ids == PASSING_IDS
    assert c.log.read_text().count("cache=hit") == 1
    assert len(list(c.cache.glob("*.json"))) == 2


def test_baseline_cache_expires_after_six_hours(baseline_case, monkeypatch):
    c = baseline_case
    c.prepare({LAST: gated_test("BASELINE_FEATURE")})
    monkeypatch.delenv("BASELINE_FEATURE", raising=False)
    assert c.capture().node_ids == PASSING_IDS[:1]
    entry, = c.cache.glob("*.json")
    assert json.loads(entry.read_text())["created_at"] == BASE_TIME.isoformat()

    monkeypatch.setenv("BASELINE_FEATURE", "enabled")
    c.now[0] += timedelta(hours=6, microseconds=-1)
    assert c.capture().node_ids == PASSING_IDS[:1]
    c.now[0] += timedelta(microseconds=1)
    assert c.capture().node_ids == PASSING_IDS
    assert json.loads(entry.read_text())["created_at"] == c.now[0].isoformat()
    assert c.capture().node_ids == PASSING_IDS
    assert c.log.read_text().count("cache=hit") == 2


@pytest.mark.parametrize("payload", [
    list(PASSING_IDS),
    {"node_ids": list(PASSING_IDS)},
    {"node_ids": list(PASSING_IDS), "created_at": "invalid"},
    {"node_ids": list(PASSING_IDS), "created_at": BASE_TIME.replace(tzinfo=None).isoformat()},
    {"node_ids": list(PASSING_IDS), "created_at": (BASE_TIME + timedelta(hours=1)).isoformat()},
])
def test_baseline_cache_rebuilds_entries_without_valid_creation_time(baseline_case, payload):
    c = baseline_case
    assert c.capture().node_ids == PASSING_IDS
    entry, = c.cache.glob("*.json")
    entry.write_text(json.dumps(payload))
    assert c.capture().node_ids == PASSING_IDS
    assert "cache=hit" not in c.log.read_text()
    assert json.loads(entry.read_text())["created_at"] == BASE_TIME.isoformat()


@pytest.mark.parametrize("options", ["-x", "--maxfail=1", "-x --deselect=" + PASSING_IDS[1]])
def test_baseline_clears_pytest_addopts_before_discovery(baseline_case, monkeypatch, options):
    c = baseline_case
    c.prepare({FIRST: "def test_first():\n    assert False\n"})
    monkeypatch.setenv("PYTEST_ADDOPTS", options)
    assert c.capture().node_ids == PASSING_IDS[1:]
    assert c.capture().node_ids == PASSING_IDS[1:]
    assert len(list(ET.parse(c.report).iter("testcase"))) == 2
    assert c.log.read_text().count("cache=hit") == 1


@pytest.mark.parametrize("options", ["-x", "--maxfail 1", "--maxfail=1", "-qx"])
def test_baseline_disables_command_early_stopping(baseline_case, options):
    c = baseline_case
    c.prepare({FIRST: "def test_first():\n    assert False\n"})
    command = COMMAND + " " + options
    assert c.capture(command).node_ids == PASSING_IDS[1:]
    assert c.capture(command).node_ids == PASSING_IDS[1:]
    assert c.log.read_text().count("cache=hit") == 1


def test_baseline_clears_inline_pytest_addopts(baseline_case):
    c = baseline_case
    c.prepare({FIRST: "def test_first():\n    assert False\n"})
    command = "PYTEST_ADDOPTS='-x' " + COMMAND
    assert c.capture(command).node_ids == PASSING_IDS[1:]
    assert c.capture(command).node_ids == PASSING_IDS[1:]
    assert c.log.read_text().count("cache=hit") == 1


@pytest.mark.parametrize("terminal", [True, False])
def test_baseline_does_not_cache_early_stop_from_pytest_plugin(baseline_case, terminal):
    c = baseline_case
    c.prepare({"conftest.py": (
        "def pytest_runtest_teardown(item):\n"
        "    item.session.shouldfail = 'stop with a partial JUnit report'\n"
    )})
    command = COMMAND if terminal else f"{shlex.quote(sys.executable)} -m pytest -p no:terminal"
    for _ in range(2):
        with pytest.raises(AttemptFailure, match="acceptance baseline unavailable"):
            c.capture(command)
    assert len(list(ET.parse(c.report).iter("testcase"))) == 1
    assert not list(c.cache.glob("*.json"))
    assert c.log.read_text().count("Acceptance baseline count=1, exit_code=1") == 2


def test_baseline_does_not_cache_duplicate_junit_records_after_early_stop(baseline_case):
    c = baseline_case
    c.prepare({
        FIRST: "def test_first():\n    assert False, 'body failed'\n",
        "conftest.py": (
            "def pytest_runtest_teardown(item):\n"
            "    item.session.shouldfail = 'stop with a partial JUnit report'\n"
            "    assert False, 'teardown failed'\n"
        ),
    })
    with pytest.raises(AttemptFailure, match="acceptance baseline unavailable"):
        c.capture()
    cases = list(ET.parse(c.report).iter("testcase"))
    assert len(cases) == 2
    assert {case.get("name") for case in cases} == {"test_first"}
    assert not list(c.cache.glob("*.json"))
    with pytest.raises(AttemptFailure, match="acceptance baseline unavailable"):
        c.capture()
    assert c.log.read_text().count("Acceptance baseline count=0, exit_code=1") == 2
    assert "cache=hit" not in c.log.read_text()


def test_baseline_collection_side_effects_do_not_pollute_execution(baseline_case):
    c = baseline_case
    c.prepare({FIRST: (
        "from pathlib import Path\n"
        "marker = Path('import-marker')\n"
        "first_import = not marker.exists()\n"
        "marker.write_text('imported')\n"
        "def test_first():\n"
        "    assert first_import\n"
    )})
    assert c.capture().node_ids == PASSING_IDS
    assert c.capture().node_ids == PASSING_IDS
    assert c.log.read_text().count("Done when command:") == 1
    assert c.log.read_text().count("cache=hit") == 1
    assert not (c.root / "import-marker").exists()


@pytest.mark.parametrize("failed", [False, True])
def test_baseline_caches_junit_without_terminal_summary(baseline_case, failed):
    c = baseline_case
    if failed:
        c.prepare({FIRST: "def test_first():\n    assert False\n"})
    command = f"{shlex.quote(sys.executable)} -m pytest -p no:terminal"
    expected = PASSING_IDS[1:] if failed else PASSING_IDS
    assert c.capture(command).node_ids == expected
    assert c.capture(command).node_ids == expected
    assert len(list(ET.parse(c.report).iter("testcase"))) == 2
    assert c.log.read_text().count("cache=hit") == 1


def test_baseline_caches_empty_passing_set_after_complete_execution(baseline_case):
    c = baseline_case
    c.prepare({
        FIRST: "def test_first():\n    assert False\n",
        LAST: "def test_last():\n    assert False\n",
    })
    assert c.capture().node_ids == ()
    assert c.capture().node_ids == ()
    assert len(list(ET.parse(c.report).iter("testcase"))) == 2
    assert c.log.read_text().count("cache=hit") == 1
