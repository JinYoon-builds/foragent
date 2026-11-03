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
