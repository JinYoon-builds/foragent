"""The docs owner's new-mechanism signal (r1317): source, acknowledgement and identity cases."""

import ast
import shutil
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))
from docs_signal_world import add_group_command, add_place, docs_requests, docs_world, merge, names, tick  # noqa: E402
from test_role_signal_raise import answer  # noqa: E402
from test_role_source import NOW  # noqa: E402
from test_skills_autosync import isolated_home  # noqa: E402,F401

HOUR = timedelta(hours=1)
PACKAGE = Path(__file__).resolve().parents[1] / "src" / "foragent"


def test_a_place_the_map_already_mentions_in_prose_is_still_signalled(tmp_path, monkeypatch):
    case = docs_world(tmp_path, monkeypatch)
    path = case.root / "docs/mechanisms.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nNightly maintenance keeps the box small.\n",
                    encoding="utf-8")
    assert tick(case, NOW) == []
    add_place(case, "maintenance")
    merge(case, 1, NOW + HOUR, ["src/foragent/shelf.py"])
    raised = tick(case, NOW + HOUR + timedelta(minutes=5))
    assert len(raised) == 1 and names(raised[0]["text"], "maintenance"), docs_requests(case)


def test_a_place_added_while_a_request_is_open_is_named_after_that_answer(tmp_path, monkeypatch):
    case = docs_world(tmp_path, monkeypatch)
    assert tick(case, NOW) == []
    add_place(case, "firstplace")
    merge(case, 1, NOW + HOUR, ["src/foragent/shelf.py"])
    [first] = tick(case, NOW + HOUR + timedelta(minutes=5))
    add_place(case, "secondplace")
    merge(case, 2, NOW + 2 * HOUR, ["src/foragent/shelf.py"])
    assert tick(case, NOW + 2 * HOUR + timedelta(minutes=5)) == [], "one open request per signal"
    case.clock[0] = NOW + 3 * HOUR
    assert answer(case, first["id"], "called", "checked firstplace") == 0
    [second] = tick(case, NOW + 3 * HOUR + timedelta(minutes=5))
    assert names(second["text"], "secondplace") and not names(second["text"], "firstplace"), second["text"]
    case.clock[0] = NOW + 4 * HOUR
    assert answer(case, second["id"], "called", "checked secondplace") == 0
    assert tick(case, NOW + 5 * HOUR) == [], "both answered: nothing left to name"
    assert len(docs_requests(case)) == 2


def test_an_answered_new_group_does_not_come_back_through_its_subcommands(tmp_path, monkeypatch):
    case = docs_world(tmp_path, monkeypatch)
    assert tick(case, NOW) == []
    add_group_command(case, tmp_path, "groupprobe")
    try:
        merge(case, 1, NOW + HOUR, ["src/foragent/commands/groupprobe.py"])
        [first] = tick(case, NOW + HOUR + timedelta(minutes=5))
        assert names(first["text"], "groupprobe") and "groupprobe " not in first["text"], first["text"]
        case.clock[0] = NOW + 2 * HOUR
        assert answer(case, first["id"], "called", "checked the group") == 0
        assert tick(case, NOW + 3 * HOUR) == [], "the group's subcommands were covered by that answer"
    finally:
        case.forget()


def _git(root, *args, **env):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True,
                          env={**_ENV, **env}).stdout.strip()


_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}


def _publish(root):
    """Make the working tree's src/foragent the merged ``origin/main``."""
    _git(root, "add", "-f", "src/foragent")
    tree = _git(root, "write-tree")
    commit = _git(root, "commit-tree", tree, "-m", "merged")
    _git(root, "update-ref", "refs/remotes/origin/main", commit)


def test_the_inventory_comes_from_the_merged_source_not_the_running_package(tmp_path, monkeypatch):
    case = docs_world(tmp_path, monkeypatch)
    if not (case.root / ".git").exists():
        _git(case.root, "init", "-q")
    shutil.copytree(PACKAGE, case.root / "src/foragent", ignore=shutil.ignore_patterns("__pycache__"))
    _publish(case.root)
    assert tick(case, NOW) == []
    tree = ast.parse((PACKAGE / "commands/bench.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "bench":
            node.value = "mergedprobe"
    (case.root / "src/foragent/commands/mergedprobe.py").write_text(ast.unparse(tree) + "\n", encoding="utf-8")
    _publish(case.root)
    merge(case, 1, NOW + HOUR, ["src/foragent/commands/mergedprobe.py"])
    raised = tick(case, NOW + HOUR + timedelta(minutes=5))
    assert len(raised) == 1 and names(raised[0]["text"], "mergedprobe"), docs_requests(case)
    assert tick(case, NOW + HOUR + timedelta(minutes=10)) == []
    # A second command reaches main with no merge record in the last 24 h: still named, not absorbed.
    case.clock[0] = NOW + 2 * HOUR
    assert answer(case, raised[0]["id"], "called", "checked mergedprobe") == 0
    module = case.root / "src/foragent/commands/laterprobe.py"
    module.write_text(ast.unparse(tree).replace("mergedprobe", "laterprobe") + "\n", encoding="utf-8")
    _publish(case.root)
    [later] = tick(case, NOW + 30 * HOUR)
    assert names(later["text"], "laterprobe") and not names(later["text"], "mergedprobe"), later["text"]
