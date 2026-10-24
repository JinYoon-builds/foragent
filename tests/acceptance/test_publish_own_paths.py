"""Acceptance test for publish-own-paths. Locked by Foragent; do not edit."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import re
from threading import Event, local

from conftest import ACCEPTANCE, git_command
import test_order as order_fixtures
from test_order import offline_publication, plan
from test_publish_out_of_scope import publish_cli
from test_skills_autosync import isolated_home


def with_clean_preread(main, monkeypatch):
    """Publishing with --approve needs a clean preread (#237). These tests are about
    other behaviour, so record one with a worker that has no unknowns first. Before
    the preread command exists, the extra call is a no-op."""
    from foragent import executors
    from foragent.executors import ExecResult

    class NoUnknowns:
        def run(self, **kwargs):
            return ExecResult(0, False, "NONE", kwargs.get("log_path"))

    import foragent.cli as cli_module
    import foragent.order as order_module

    for module in (executors, cli_module, order_module):
        if hasattr(module, "executor_for"):
            monkeypatch.setattr(module, "executor_for", lambda name: NoUnknowns())

    def wrapped(argv):
        if argv[:2] == ["order", "publish"] and "--approve" in argv and "--dry-run" not in argv:
            extra = ["--repo-root", argv[argv.index("--repo-root") + 1]] if "--repo-root" in argv else []
            try:
                main(["order", "preread", argv[2], *extra])
            except SystemExit:
                pass  # No preread command yet: nothing to record.
        return main(argv)

    return wrapped


def draft_plan(directory, value):
    """The interviewer supplies a plan outside the shared clone."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{value['id']}.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def owned_files(value):
    return {
        f"docs/specs/{value['id']}.md": value["spec_markdown"],
        **{name: content for issue in value["issues"]
           for name, content in issue.get("acceptance_files", {}).items()},
    }


def test_publish_own_paths_preserves_other_session_work_on_latest_main(
    offline_publication, git_history, plan, monkeypatch, capsys,
):
    # Scope isolation already works on an unchanged main. Exercise it together
    # with remote advancement so this regression test is red on current code.
    root, _, _, _, _, origin = offline_publication
    main = with_clean_preread(publish_cli(offline_publication[:5], monkeypatch), monkeypatch)
    path = draft_plan(root.parent / "interview-drafts", plan)
    initial = git_command(root, "rev-parse", "HEAD")
    tracked = root / ACCEPTANCE
    original = tracked.read_bytes()
    tracked.write_bytes(b"# Another session's unfinished tracked edit.\n")
    untracked_name = "tests/acceptance/test_other_session_draft.py"
    untracked = root / untracked_name
    untracked.write_bytes(b"def test_other_session(\n")
    contents = {name: (root / name).read_bytes() for name in (ACCEPTANCE, untracked_name)}
    status = git_command(root, "status", "--porcelain=v1", "--untracked-files=all",
                         "--", *contents)

    # Import an actual competing commit into the bare origin, without fetching
    # it into the shared clone. The fixture uses fast-import, never git push.
    remote_file = "api/another_session.py"
    git_history.update({remote_file: "# Another session's completed work.\n"})
    latest_main = git_history.tip
    assert latest_main != initial
    assert git_command(root, "rev-parse", "origin/main") == initial

    result = main(["order", "publish", str(path), "--repo-root", str(root), "--approve"])
    output = capsys.readouterr()
    assert {name: (root / name).read_bytes() for name in contents} == contents
    assert git_command(root, "status", "--porcelain=v1", "--untracked-files=all",
                       "--", *contents) == status
    assert result == 0, f"publish must succeed on moved main without manual rebase: {output.err}"

    tip = git_command(origin, "rev-parse", "main")
    assert git_command(origin, "rev-parse", f"{tip}^") == latest_main
    assert git_command(origin, "rev-list", "--count", f"{initial}..{tip}") == "2"
    assert git_command(root, "rev-parse", "HEAD") == tip
    assert git_command(origin, "show", f"{tip}:{remote_file}") == "# Another session's completed work."
    expected = owned_files(plan)
    changed = git_command(origin, "diff-tree", "--no-commit-id", "--name-only", "-r", tip)
    assert set(changed.splitlines()) == set(expected), "publish must commit exactly its own paths"
    for name, content in expected.items():
        assert git_command(origin, "show", f"{tip}:{name}") == content.strip()
    assert git_command(origin, "show", f"{tip}:{ACCEPTANCE}") == original.decode().strip()
    assert untracked_name not in git_command(origin, "ls-tree", "-r", "--name-only", tip).splitlines()


def test_publish_own_paths_simultaneous_publishes_both_land_in_order(
    offline_publication, plan, monkeypatch, capsys,
):
    root, _, tracker, _, _, origin = offline_publication
    main = with_clean_preread(publish_cli(offline_publication[:5], monkeypatch), monkeypatch)
    initial = git_command(origin, "rev-parse", "main")
    second_plan = deepcopy(plan)
    second_plan["id"] = "2026-10-23-second-order"
    second_plan["title"] = "Another independent order"
    second_plan["spec_markdown"] = "# Another independent spec\n"
    second_acceptance = "tests/acceptance/test_second_order.py"
    second_plan["issues"][0]["acceptance_files"] = {
        second_acceptance: "def test_second_order():\n    assert True\n",
    }
    second_plan["issues"][0]["done_when"] = [f"uv run pytest -q {second_acceptance}"]
    paths = [draft_plan(root.parent / "interview-drafts", value) for value in (plan, second_plan)]
    first_snapshot, second_finished = Event(), Event()
    worker = local()

    def paused_git_command(directory, *args, **kwargs):
        value = git_command(directory, *args, **kwargs)
        if directory == root and args == ("rev-parse", "HEAD") and getattr(worker, "pause", False):
            # Pause the EXISTING offline commit transport after reading its real
            # parent. An unlocked publisher can now race that parent; a locked
            # publisher waits until this bounded pause ends. No error is mocked.
            worker.pause = False
            first_snapshot.set()
            second_finished.wait(timeout=5)
        return value

    monkeypatch.setattr(order_fixtures, "git_command", paused_git_command)

    def invoke(path, *, first=False):
        worker.pause = first
        try:
            return main(["order", "publish", str(path), "--repo-root", str(root), "--approve"])
        finally:
            if not first:
                second_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(invoke, paths[0], first=True)
        assert first_snapshot.wait(timeout=10), "first publish did not reach the Git commit transport"
        second = pool.submit(invoke, paths[1])
        results = [first.result(timeout=20), second.result(timeout=20)]

    output = capsys.readouterr()
    assert results == [0, 0], f"both simultaneous publishes must succeed: {results}; {output.err}"
    commits = git_command(origin, "rev-list", "--reverse", f"{initial}..main").splitlines()
    assert len(commits) == 2, "each independent order must land as its own commit"
    previous = initial
    remaining = [owned_files(plan), owned_files(second_plan)]
    for commit in commits:
        assert git_command(origin, "rev-parse", f"{commit}^") == previous
        changed = set(git_command(origin, "diff-tree", "--no-commit-id", "--name-only", "-r", commit).splitlines())
        matches = [files for files in remaining if set(files) == changed]
        assert len(matches) == 1, "a publish must contain only the files of one order"
        remaining.remove(matches[0])
        previous = commit
    assert not remaining
    assert git_command(root, "rev-parse", "HEAD") == commits[-1]
    for value in (plan, second_plan):
        for name, content in owned_files(value).items():
            assert git_command(origin, "show", f"main:{name}") == content.strip()
        assert any(f"foragent:plan id={value['id']} " in issue.body for issue in tracker.list_issues())


def test_publish_own_paths_skill_drafts_outside_main_and_delegates_placement():
    skill = Path(__file__).resolve().parents[2] / "skills/fa-order/SKILL.md"
    sections = [" ".join(section.lower().replace("`", "").split())
                for section in re.split(r"(?m)^#{1,6}\s+", skill.read_text(encoding="utf-8"))]
    concepts = (
        ("acceptance", "test files", "tests"),
        ("spec",),
        ("draft", "write", "prepare"),
        ("temporary", "temp folder", "temp directory", "/tmp", "mktemp"),
        ("outside", "away from", "separate from"),
        ("checkout", "working tree", "repository", "repo"),
    )
    assert any(all(any(term in section for term in alternatives) for alternatives in concepts)
               for section in sections), (
        "fa-order must tell the interviewer to draft acceptance tests and specs in a temporary "
        "folder outside the main checkout"
    )
    assert any("fa order publish" in section and "acceptance" in section and "spec" in section
               and any(verb in section for verb in ("place", "materializ", "copy", "copies", "write", "install"))
               for section in sections), (
        "fa-order must delegate placing the acceptance files and spec in the checkout to fa order publish"
    )
