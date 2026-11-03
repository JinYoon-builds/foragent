"""Locked publish-collect-check contract: public CLI, real collection, offline I/O.

Only Git commit/push transport, GitHub and the preread model are faked. No
publication validation, collection result, preread binding or flush is replaced.
The repository's HOME, signal and forbidden-Git-command guards also apply here.
"""

from copy import deepcopy
import json
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace

import pytest
import yaml


ROOT = next(p for p in Path(__file__).resolve().parents if (p / "src/foragent").is_dir())
for directory in (ROOT / "tests", ROOT / "tests/acceptance"):
    if str(directory) not in sys.path:
        sys.path.append(str(directory))
pytest_plugins = [] if Path(__file__).resolve().is_relative_to(ROOT / "tests") else ["conftest"]

from conftest import git_command, git_history  # noqa: E402, F401
from foragent.executors import ExecResult  # noqa: E402
from foragent.git import GitRepo  # noqa: E402
from foragent.state import StateStore  # noqa: E402
from foragent.tracker import FakeTracker  # noqa: E402


REPO = "owner/repo"
LOCKED = "tests/acceptance/test_collect_candidate.py"
HELPER = "tests/acceptance/publish_collect_fixture_helper.py"
HELPER_TEXT = "TOKEN = 'included in this order'\n"
BOUNDARY = "collect-check-boundary"
WITNESS = (
    "import os\n"
    "assert os.environ.get('FA_COLLECTION_PROJECT_COMMAND') == 'selected', "
    "'collection must use the configured project command'\n"
)
IMPORTING = WITNESS + (
    "from publish_collect_fixture_helper import TOKEN\n\n"
    "def test_candidate():\n"
    "    assert TOKEN == 'included in this order'\n"
)
RED = WITNESS + "\ndef test_candidate():\n    assert False, 'red until implemented'\n"
MUTATIONS = (
    "ensure_labels", "create_issue", "update_issue", "add_labels", "remove_labels",
    "comment", "close_issue", "add_sub_issue", "add_blocked_by", "remove_blocked_by",
)


@pytest.fixture
def collection_case(git_history, tmp_path, monkeypatch):
    from foragent import cli, executors, gh, order

    history, events, mutations, worker_calls = git_history, [], [], []
    root = history.clone
    command = (
        f"FA_COLLECTION_PROJECT_COMMAND=selected {shlex.quote(sys.executable)} -m pytest "
        "-q tests/test_unrelated.py"
    )
    settings = {
        "repo": REPO, "language": "en", "review": "fast", "executor": "codex",
        "skill_libraries": [], "test_command": command, "test_slots": 1,
    }
    history.update({
        ".foragent.yml": yaml.safe_dump(settings),
        "pyproject.toml": "[tool.pytest.ini_options]\n",
        # Any accidental full-suite collection fails even without running tests.
        "tests/test_unrelated.py": "raise RuntimeError('unrelated files must not be collected')\n",
    })
    GitRepo(root).update_base("main")

    def transport(args):
        """Store real Git trees and move only the fixture's local bare origin."""
        directory = root
        if args[:1] == ["-C"]:
            directory, args = Path(args[1]), args[2:]
        if "commit" in args:
            events.append("commit")
            title = args[args.index("-m") + 1]
            parent = git_command(directory, "rev-parse", "HEAD")
            stream = [
                "commit refs/heads/main\n",
                "committer Offline Tests <tests@example.com> 1792483200 +0000\n",
                f"data {len(title.encode())}\n{title}\n", f"from {parent}\n",
            ]
            for name in args[args.index("--") + 1:]:
                if name.startswith(":!"):
                    continue
                entry = git_command(directory, "ls-files", "--stage", "-z", "--", name)
                metadata, filename = entry.rstrip("\0").split("\t", 1)
                mode, sha, stage = metadata.split()
                assert stage == "0"
                stream.append(f"M {mode} {sha} {json.dumps(filename)}\n")
            git_command(directory, "fast-import", "--quiet", input="".join(stream) + "\ndone\n")
            return ""
        if args[:1] == ["push"]:
            events.append("push")
            source, destination = args[-1].split(":")
            git_command(history.origin, "fetch", str(directory), f"{source}:{destination}")
            return ""
        return git_command(directory, *args)

    git = GitRepo(root, transport)
    tracker = FakeTracker(actor="human")
    for name in MUTATIONS:
        original = getattr(tracker, name)

        def mutate(*args, _name=name, _original=original, **kwargs):
            mutations.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(tracker, name, mutate)

    class Reader:
        def run(self, **kwargs):
            worker_calls.append(kwargs)
            return ExecResult(0, False, "NONE", kwargs["log_path"])

    for module in (executors, cli, order):
        if hasattr(module, "executor_for"):
            monkeypatch.setattr(module, "executor_for", lambda name: Reader())

    def no_github(*args, **kwargs):
        pytest.fail("The acceptance fixture must not call GitHub")

    monkeypatch.setattr(gh, "run", no_github)
    monkeypatch.setattr(cli, "GhTracker", lambda repo: tracker)
    monkeypatch.setattr(cli, "GitRepo", lambda path: git if Path(path) == root else GitRepo(path))
    monkeypatch.chdir(root)
    # Tests are about the project under publication, not this Foragent worktree.
    monkeypatch.delenv("PYTHONPATH", raising=False)
    value = {
        "id": "offline-collect-candidate", "title": "Collection candidate",
        "summary": "Keep published acceptance files collectable.",
        "spec_markdown": "# Collection candidate\nPublish the locked candidate.\n",
        "issues": [{
            "key": "candidate", "title": "Implement candidate", "lane": "main", "size": "s",
            "goal": "Implement the candidate.", "out_of_scope": "Unrelated behavior.",
            "files": ["candidate.py", LOCKED],
            "done_when": [f"uv run pytest -q {LOCKED}"],
            "acceptance_files": {LOCKED: IMPORTING},
        }],
    }
    path = tmp_path / "candidate.json"
    store = StateStore.for_repo(REPO)
    case = SimpleNamespace(
        root=root, history=history, git=git, tracker=tracker, main=cli.main,
        events=events, mutations=mutations, worker_calls=worker_calls,
        plan=value, path=path, store=store,
    )
    # This dependency is a dirty local file, absent from origin and the order.
    helper = root / HELPER
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text(HELPER_TEXT)
    return case


def call(case, capsys, action, *options):
    case.path.write_text(json.dumps(case.plan))
    argv = ["order", action]
    if action in {"publish", "preread"}:
        argv.append(str(case.path))
    argv.extend(["--repo-root", str(case.root), *options])
    capsys.readouterr()
    try:
        code = case.main(argv)
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def snapshot(case):
    paths = [LOCKED, f"docs/specs/{case.plan['id']}.md", ".foragent/preread.json"]
    return {
        "origin": git_command(case.history.origin, "rev-parse", "refs/heads/main"),
        "commits": deepcopy(case.events), "mutations": deepcopy(case.mutations),
        "issues": deepcopy(case.tracker.list_issues()),
        "index": git_command(case.root, "ls-files", "--stage"),
        "files": {name: (case.root / name).read_bytes() if (case.root / name).exists() else None
                  for name in paths},
        "waiting": (case.store.root / "orders.jsonl").read_bytes()
                   if (case.store.root / "orders.jsonl").exists() else None,
    }


def assert_refusal(case, capsys, action, *options):
    before, readers = snapshot(case), len(case.worker_calls)
    code, diagnostic = call(case, capsys, action, *options)
    assert code != 0, f"{action} accepted an uncollectable publication: {diagnostic}"
    assert LOCKED in diagnostic, "Collection refusal must name the locked file"
    assert "ModuleNotFoundError" in diagnostic and "publish_collect_fixture_helper" in diagnostic, diagnostic
    assert snapshot(case) == before, "Refusal changed publication files, index, origin, issues or waiting orders"
    assert len(case.worker_calls) == readers, "A collection refusal must precede the preread model"


def prime_clean_preread_then_remove_base_helper(case, capsys):
    """The same plan has a clean preread before its base dependency disappears."""
    (case.root / HELPER).unlink()
    case.history.update({HELPER: HELPER_TEXT})
    case.git.update_base("main")
    code, diagnostic = call(case, capsys, "preread", "--json")
    assert code == 0, diagnostic
    assert len(case.worker_calls) == 1
    case.history.update({HELPER: None})
    # Keep the preread, but make the deleted dependency local dirt.
    case.git.update_base("main")
    (case.root / HELPER).write_text(HELPER_TEXT)


@pytest.mark.parametrize("approve", [False, True])
@pytest.mark.parametrize("deferred", [False, True])
def test_missing_helper_is_refused_before_publication(collection_case, capsys, approve, deferred):
    case = collection_case
    if approve:
        prime_clean_preread_then_remove_base_helper(case, capsys)
    options = (["--approve"] if approve else []) + (["--defer", BOUNDARY] if deferred else [])
    assert_refusal(case, capsys, "publish", *options)


@pytest.mark.parametrize("action,options", [("publish", ["--dry-run"]), ("preread", ["--json"])])
def test_dry_run_and_preread_report_the_collection_error(collection_case, capsys, action, options):
    assert_refusal(collection_case, capsys, action, *options)


@pytest.mark.parametrize("runtime_red", [False, True])
def test_collectable_files_publish_even_when_runtime_is_red(collection_case, capsys, runtime_red):
    case = collection_case
    item = case.plan["issues"][0]
    if runtime_red:
        item["acceptance_files"] = {LOCKED: RED}
    else:
        item["acceptance_files"][HELPER] = HELPER_TEXT
        # A caller's conflicting helper must not replace the locked overlay.
        (case.root / HELPER).write_text("raise RuntimeError('dirty caller helper')\n")
    before = git_command(case.history.origin, "rev-parse", "main")
    code, diagnostic = call(case, capsys, "publish")
    assert code == 0, diagnostic
    assert case.events == ["commit", "push"]
    assert git_command(case.history.origin, "rev-parse", "main") != before
    for name, content in item["acceptance_files"].items():
        assert git_command(case.history.origin, "show", f"main:{name}") == content.strip()
    assert len(case.tracker.list_issues()) == 2
    assert case.worker_calls == [], "Unapproved publication does not need a model"


def test_flush_revalidates_retained_files_on_current_base_before_committing(collection_case, capsys):
    case = collection_case
    (case.root / HELPER).unlink()
    case.history.update({HELPER: HELPER_TEXT})
    case.git.update_base("main")
    code, diagnostic = call(case, capsys, "publish", "--defer", BOUNDARY)
    assert code == 0, diagnostic
    assert case.events == [] and case.tracker.list_issues() == []
    # A valid retained entry becomes invalid on a later main. Refresh the caller
    # too, so the assertion isolates flush validation rather than base movement.
    case.history.update({HELPER: None})
    case.git.update_base("main")
    (case.root / HELPER).write_text(HELPER_TEXT)
    assert_refusal(case, capsys, "flush", "--boundary", BOUNDARY, "--json")


def test_collection_uses_remote_base_even_when_local_helper_still_exists(collection_case, capsys):
    case = collection_case
    (case.root / HELPER).unlink()
    case.history.update({HELPER: HELPER_TEXT})
    case.git.update_base("main")
    case.history.update({HELPER: None})
    # Local HEAD and its tracked helper deliberately stay behind origin/main.
    assert (case.root / HELPER).exists()
    before = snapshot(case)
    code, diagnostic = call(case, capsys, "publish", "--dry-run")
    assert code != 0, "Dry-run trusted stale local main instead of the current remote base"
    assert LOCKED in diagnostic and "ModuleNotFoundError" in diagnostic, diagnostic
    assert snapshot(case) == before, "A refused dry-run changed the caller or origin"


def test_unavailable_collection_command_fails_closed(collection_case, capsys):
    case = collection_case
    settings = yaml.safe_load((case.root / ".foragent.yml").read_text())
    settings["test_command"] = "/__foragent_missing_collection_runner__/pytest"
    case.history.update({".foragent.yml": yaml.safe_dump(settings)})
    case.git.update_base("main")
    case.plan["issues"][0]["acceptance_files"] = {LOCKED: "def test_candidate():\n    assert True\n"}
    before = snapshot(case)
    code, diagnostic = call(case, capsys, "publish")
    assert code != 0, "Unavailable collection must not be treated as a pass"
    assert "collect" in diagnostic.lower() and "pytest" in diagnostic, diagnostic
    assert snapshot(case) == before


def test_unavailable_current_base_fails_closed_instead_of_using_local_main(collection_case, capsys):
    case = collection_case
    # This is a missing local remote, never a live network request. A fallback
    # to the stale local branch would falsely certify a current-base check.
    git_command(case.root, "config", "remote.origin.url", str(case.root / "unavailable-origin.git"))
    case.plan["issues"][0]["acceptance_files"] = {LOCKED: "def test_candidate():\n    assert True\n"}
    before = snapshot(case)
    code, diagnostic = call(case, capsys, "publish", "--dry-run")
    assert code != 0, "An unreadable current base is unknown, not a successful collection"
    assert "collect" in diagnostic.lower() and LOCKED in diagnostic, diagnostic
    assert "base" in diagnostic.lower() or "origin" in diagnostic.lower(), diagnostic
    assert snapshot(case) == before


# --- Lane test runs hold every machine test slot (2026-11-03: five refusals) ---------------------
# The holder is a real `fa test` run of a lane test that keeps going until this test releases it.
# The project allows one test slot, so that one run holds every slot the checks could take.
LANE_TEST = (
    "import pathlib, time\n\n"
    "def test_lane_work_in_progress():\n"
    "    here = pathlib.Path(__file__).parent\n"
    "    (here / 'started').write_text('running')\n"
    "    end = time.monotonic() + 300\n"
    "    while not (here / 'release').exists() and time.monotonic() < end:\n"
    "        time.sleep(0.05)\n"
    "    (here / 'finished').write_text('done')\n"
)


@pytest.fixture
def lane_tests_hold_every_slot(collection_case, tmp_path):
    import threading
    import time

    case = collection_case
    lane = tmp_path / "lane-run"
    lane.mkdir()
    (lane / "test_lane_work.py").write_text(LANE_TEST)
    result = {}

    def run():
        try:
            result["code"] = case.main([
                "test", "--repo-root", str(case.root), "--",
                "-q", "-p", "no:cacheprovider", "--rootdir", str(lane), str(lane / "test_lane_work.py"),
            ])
        except SystemExit as exc:
            result["code"] = exc.code

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    end = time.monotonic() + 120
    while not (lane / "started").exists() and thread.is_alive() and time.monotonic() < end:
        time.sleep(0.05)
    assert (lane / "started").exists(), f"The lane test run never started: {result}"
    holder = SimpleNamespace(directory=lane, running=lambda: not (lane / "finished").exists())
    try:
        yield holder
    finally:
        (lane / "release").write_text("go")
        thread.join(60)
    assert result.get("code") == 0, f"The lane test run itself failed: {result}"


def test_collect_check_passes_while_lane_tests_hold_every_slot(collection_case, lane_tests_hold_every_slot,
                                                                capsys):
    case, holder = collection_case, lane_tests_hold_every_slot
    item = case.plan["issues"][0]
    item["acceptance_files"][HELPER] = HELPER_TEXT
    for action, options in (("publish", ["--dry-run"]), ("preread", ["--json"])):
        code, diagnostic = call(case, capsys, action, *options)
        assert code == 0, f"{action} {' '.join(options)} was refused while lane tests ran: {diagnostic}"
        assert holder.running(), f"{action} waited for the lane test run to finish"
    before = git_command(case.history.origin, "rev-parse", "main")
    code, diagnostic = call(case, capsys, "publish")
    assert code == 0, f"publish was refused while lane tests ran: {diagnostic}"
    assert holder.running(), "publish waited for the lane test run to finish"
    assert git_command(case.history.origin, "rev-parse", "main") != before
    assert git_command(case.history.origin, "show", f"main:{LOCKED}") == IMPORTING.strip()
    assert len(case.tracker.list_issues()) == 2


@pytest.mark.parametrize("action,options", [
    ("publish", ["--defer", BOUNDARY]), ("publish", ["--dry-run"]), ("preread", ["--json"]),
])
def test_uncollectable_file_is_still_refused_while_lane_tests_hold_every_slot(
        collection_case, lane_tests_hold_every_slot, capsys, action, options):
    assert_refusal(collection_case, capsys, action, *options)
    assert lane_tests_hold_every_slot.running(), f"{action} waited for the lane test run to finish"


# --- A changed shared test file breaks other tests that import it (2026-11-03) -------------------
# An order rewrote the shared acceptance file's NAMES/CHECKLIST. Its own new tests were red on main
# as expected, but a unit test that imported CHECKLIST and perspective_case from it, and passed on
# main, broke once the order landed: main CI was red for about 50 minutes. The small project below
# has the same shape: a shared acceptance file, a passing importer and an importer red on main.
SHARED = "tests/acceptance/test_review_perspectives.py"
IMPORTER = "tests/test_review_conflict_paths.py"
IMPORTER_TEST = "test_panel_has_one_verdict_per_perspective"
RED_IMPORTER = "tests/test_review_already_red.py"
PRODUCT = "PERSPECTIVES = ('goal-fidelity', 'concurrency', 'interruption-rollback', 'test-quality')\n"
SHARED_PATHS = (
    "import pathlib, sys\n\n"
    "ROOT = pathlib.Path(__file__).resolve().parents[2]\n"
    "if str(ROOT) not in sys.path:\n"
    "    sys.path.insert(0, str(ROOT))\n\n"
    "import product\n\n"
)
SHARED_BASE = SHARED_PATHS + (
    "NAMES = ('goal-fidelity', 'concurrency', 'interruption-rollback', 'test-quality')\n"
    "CHECKLIST = tuple(f'{name}: checked' for name in NAMES)\n\n\n"
    "def perspective_case(count=2):\n"
    "    return {'reviewers': count, 'perspectives': list(NAMES)}\n\n\n"
    "def test_review_perspectives_defaults():\n"
    "    assert product.PERSPECTIVES == NAMES\n"
)
# The same kind of change the incident made: new names the product does not have yet.
SHARED_RENAMED = SHARED_PATHS + (
    "NAMES = ('rule-documents', 'obvious-bugs', 'git-history', 'earlier-pr-comments', 'code-comments',\n"
    "         'fatal-path')\n"
    "CHECKLIST = tuple(f'{name}: checked' for name in NAMES)\n\n\n"
    "def perspective_case(count=2):\n"
    "    return {'reviewers': count, 'perspectives': list(NAMES)}\n\n\n"
    "def test_review_perspectives_defaults():\n"
    "    assert product.PERSPECTIVES == NAMES\n"
)
# A shared name the importer needs is gone, so the importer no longer imports at all.
SHARED_WITHOUT_CASE = SHARED_PATHS + (
    "NAMES = ('goal-fidelity', 'concurrency', 'interruption-rollback', 'test-quality')\n"
    "CHECKLIST = tuple(f'{name}: checked' for name in NAMES)\n\n\n"
    "def test_review_perspectives_defaults():\n"
    "    assert product.PERSPECTIVES == NAMES\n"
)
# A new lock that is red on main but leaves everything importers use as it was.
SHARED_EXTENDED = SHARED_BASE + (
    "\n\nFATAL_PATH = 'fatal-path'\n\n\n"
    "def test_review_perspectives_gain_fatal_path():\n"
    "    assert FATAL_PATH in product.PERSPECTIVES, 'red until implemented'\n"
)
IMPORTER_PATHS = (
    "import pathlib, sys\n\n"
    "ROOT = pathlib.Path(__file__).resolve().parents[1]\n"
    "for directory in (ROOT, ROOT / 'tests' / 'acceptance'):\n"
    "    if str(directory) not in sys.path:\n"
    "        sys.path.insert(0, str(directory))\n\n"
    "import product\n"
    "from test_review_perspectives import CHECKLIST, perspective_case\n\n\n"
)
IMPORTER_BASE = IMPORTER_PATHS + (
    f"def {IMPORTER_TEST}():\n"
    "    case = perspective_case()\n"
    "    assert len(CHECKLIST) == len(product.PERSPECTIVES)\n"
    "    assert case['perspectives'] == list(product.PERSPECTIVES)\n"
)
RED_IMPORTER_BASE = IMPORTER_PATHS + (
    "def test_review_already_red():\n"
    "    assert len(CHECKLIST) == 99, 'red on main before any order'\n"
)


@pytest.fixture
def importer_case(collection_case):
    case = collection_case
    case.history.update({
        "product.py": PRODUCT, SHARED: SHARED_BASE, IMPORTER: IMPORTER_BASE, RED_IMPORTER: RED_IMPORTER_BASE,
    })
    case.git.update_base("main")
    item = case.plan["issues"][0]
    item["files"] = ["product.py", SHARED]
    item["done_when"] = [f"uv run pytest -q {SHARED}"]
    item["acceptance_files"] = {SHARED: SHARED_RENAMED}
    return case


def assert_importer_refusal(case, capsys, action, *options):
    before, readers = snapshot(case), len(case.worker_calls)
    shared = (case.root / SHARED).read_bytes()
    code, diagnostic = call(case, capsys, action, *options)
    assert code != 0, f"{action} accepted an order that breaks {IMPORTER}, which passes on main: {diagnostic}"
    assert IMPORTER in diagnostic, f"The refusal must name the broken importer {IMPORTER}: {diagnostic}"
    assert RED_IMPORTER not in diagnostic, f"{RED_IMPORTER} was already red on main: {diagnostic}"
    assert snapshot(case) == before, "Refusal changed publication files, index, origin, issues or waiting orders"
    assert (case.root / SHARED).read_bytes() == shared, "Refusal changed the shared file in the checkout"
    assert len(case.worker_calls) == readers, "An importer refusal must precede the preread model"
    return diagnostic


@pytest.mark.parametrize("action,options", [
    ("publish", []), ("publish", ["--dry-run"]), ("publish", ["--defer", BOUNDARY]), ("preread", ["--json"]),
])
def test_changed_shared_file_that_breaks_a_passing_importer_is_refused(importer_case, capsys, action, options):
    diagnostic = assert_importer_refusal(importer_case, capsys, action, *options)
    assert IMPORTER_TEST in diagnostic, f"The refusal must name the broken test {IMPORTER_TEST}: {diagnostic}"


def test_importer_that_no_longer_imports_is_refused(importer_case, capsys):
    case = importer_case
    case.plan["issues"][0]["acceptance_files"] = {SHARED: SHARED_WITHOUT_CASE}
    diagnostic = assert_importer_refusal(case, capsys, "publish", "--dry-run")
    assert "perspective_case" in diagnostic, f"The refusal must show why the importer broke: {diagnostic}"


def test_changed_shared_file_that_keeps_importers_passing_publishes(importer_case, capsys):
    case = importer_case
    item = case.plan["issues"][0]
    item["acceptance_files"] = {SHARED: SHARED_EXTENDED}
    for action, options in (("publish", ["--dry-run"]), ("preread", ["--json"])):
        code, diagnostic = call(case, capsys, action, *options)
        assert code == 0, f"{action} {' '.join(options)} refused an order whose importers keep passing: {diagnostic}"
    before = git_command(case.history.origin, "rev-parse", "main")
    code, diagnostic = call(case, capsys, "publish")
    assert code == 0, f"publish refused an order whose importers keep passing: {diagnostic}"
    assert case.events == ["commit", "push"]
    assert git_command(case.history.origin, "rev-parse", "main") != before
    assert git_command(case.history.origin, "show", f"main:{SHARED}") == SHARED_EXTENDED.strip()
    assert len(case.tracker.list_issues()) == 2


def test_importer_check_does_not_wait_for_lane_test_runs(importer_case, lane_tests_hold_every_slot, capsys):
    case, holder = importer_case, lane_tests_hold_every_slot
    assert_importer_refusal(case, capsys, "publish", "--dry-run")
    assert holder.running(), "The refusal waited for the lane test run to finish"
    case.plan["issues"][0]["acceptance_files"] = {SHARED: SHARED_EXTENDED}
    code, diagnostic = call(case, capsys, "publish")
    assert code == 0, f"publish was refused while lane tests ran: {diagnostic}"
    assert holder.running(), "publish waited for the lane test run to finish"
    assert git_command(case.history.origin, "show", f"main:{SHARED}") == SHARED_EXTENDED.strip()
