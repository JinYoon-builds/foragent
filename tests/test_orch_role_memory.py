"""Role memory publication: local commit before any remote read, byte-exact paths, moved base."""

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from conftest import git_command
from foragent import orch_role_memory
from foragent.config import Config
from foragent.git import GitError, GitRepo
from foragent.scheduler import TickReport

FOLDER = ".claude/agent-memory/fa-docs"
NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)


def run(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def case(git_history, tmp_path):
    """Real Git objects; commit is a path-only fast-import and push a fetch into the bare origin."""
    git_history.update({f"{FOLDER}/MEMORY.md": "Old lesson.\n", "README.md": "Project.\n"})
    root = git_history.clone
    run(root, "fetch", "origin", "main")
    run(root, "merge", "--ff-only", "origin/main")
    offline = []
    commit_failures = []

    def transport(args):
        if commit_failures and "commit" in args:
            commit_failures.pop()
            raise subprocess.CalledProcessError(1, ["git", *args], stderr="pre-commit hook failed")
        if offline and args[:1] in (["fetch"], ["push"]):
            raise subprocess.CalledProcessError(128, ["git", *args], stderr="offline")
        if "commit" in args:
            paths = [path for path in args[args.index("--") + 1:] if not path.startswith(":!")]
            title = args[args.index("-m") + 1]
            parent = git_command(root, "rev-parse", "HEAD")
            # Bytes, not text: text mode turns a "\r" inside a file name into "\n".
            staged = subprocess.run(["git", "ls-files", "--stage", "-z", "--", *paths], cwd=root, check=True,
                                    capture_output=True).stdout.decode()
            stream = ["commit refs/heads/main\n", "committer Tests <tests@example.com> 1793620800 +0000\n",
                      f"data {len(title.encode())}\n{title}\n", f"from {parent}\n"]
            stream += [f"D {json.dumps(path, ensure_ascii=False)}\n" for path in paths]
            for entry in filter(None, staged.split("\0")):
                metadata, path = entry.split("\t", 1)
                mode, sha, _ = metadata.split()
                stream.append(f"M {mode} {sha} {json.dumps(path, ensure_ascii=False)}\n")
            git_command(root, "fast-import", "--quiet", input="".join(stream) + "\ndone\n")
            return ""
        if args[:1] == ["push"]:
            source, destination = args[-1].split(":")
            git_command(git_history.origin, "fetch", str(root), f"{source}:{destination}")
            # A real push also moves the pushed branch's remote-tracking ref.
            git_command(root, "update-ref", destination.replace("refs/heads/", "refs/remotes/origin/"), source)
            return ""
        return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout

    roles = {"docs": {"agent": "fa-docs", "skills": [], "memory": FOLDER + "/"}}
    config = Config(repo="owner/repo", language="en", skill_libraries=[], roles=roles)
    clock = SimpleNamespace(value=NOW)
    ctx = SimpleNamespace(repo_root=root, config=config, git=GitRepo(root, transport),
                          store=SimpleNamespace(root=tmp_path / "state"), now=lambda: clock.value)
    return SimpleNamespace(root=root, origin=git_history.origin, history=git_history, ctx=ctx, offline=offline,
                           clock=clock, commit_failures=commit_failures)


def publish(case) -> list[str]:
    report = TickReport()
    orch_role_memory.publish(case.ctx, report)
    return report.actions


def origin_file(case, path: str) -> bytes | None:
    result = subprocess.run(["git", "-C", str(case.origin), "show", f"main:{path}"], capture_output=True)
    return result.stdout if result.returncode == 0 else None


def head_file(case, path: str) -> bytes | None:
    result = subprocess.run(["git", "-C", str(case.root), "show", f"HEAD:{path}"], capture_output=True)
    return result.stdout if result.returncode == 0 else None


def test_offline_fetch_still_commits_locally_and_a_later_tick_delivers(case):
    (case.root / FOLDER / "MEMORY.md").write_text("Old lesson.\nNew lesson.\n")
    case.offline.append(True)
    actions = publish(case)
    assert "delivery failed" in " ".join(actions)
    assert head_file(case, f"{FOLDER}/MEMORY.md") == b"Old lesson.\nNew lesson.\n"
    assert origin_file(case, f"{FOLDER}/MEMORY.md") == b"Old lesson.\n"
    case.offline.clear()
    assert publish(case) == ["role memory published to main"]
    assert origin_file(case, f"{FOLDER}/MEMORY.md") == b"Old lesson.\nNew lesson.\n"


def test_non_ascii_memory_file_is_committed_and_retried(case):
    name = f"{FOLDER}/새-교훈.md"
    (case.root / name).write_text("한글 이름의 교훈.\n")
    case.offline.append(True)
    publish(case)
    assert head_file(case, name) == "한글 이름의 교훈.\n".encode()
    case.offline.clear()
    assert publish(case) == ["role memory published to main"]
    assert origin_file(case, name) == "한글 이름의 교훈.\n".encode()
    assert publish(case) == []


def test_moved_origin_base_is_integrated_and_staged_work_stays(case):
    case.history.update({"README.md": "Project, merged elsewhere.\n"}, message="Merged")
    (case.root / "unrelated.txt").write_text("Staged by another session.\n")
    run(case.root, "add", "unrelated.txt")
    (case.root / FOLDER / "MEMORY.md").write_text("Old lesson.\nNew lesson.\n")
    assert publish(case) == ["role memory published to main"]
    assert origin_file(case, f"{FOLDER}/MEMORY.md") == b"Old lesson.\nNew lesson.\n"
    assert origin_file(case, "README.md") == b"Project, merged elsewhere.\n"
    assert origin_file(case, "unrelated.txt") is None
    assert run(case.root, "diff", "--cached", "--name-only").split() == ["unrelated.txt"]
    assert (case.root / "README.md").read_text() == "Project, merged elsewhere.\n"
    assert run(case.root, "rev-parse", "HEAD") == run(case.origin, "rev-parse", "main")


def test_unpushed_work_outside_role_memory_is_not_pushed_by_the_role_publisher(case):
    (case.root / "README.md").write_text("Local, unpublished.\n")
    case.ctx.git.commit_paths(["README.md"], "Someone else's local commit")
    (case.root / FOLDER / "MEMORY.md").write_text("Old lesson.\nNew lesson.\n")
    case.history.update({"OTHER.md": "Moved.\n"}, message="Moved")
    actions = publish(case)
    assert "delivery waits" in " ".join(actions)
    assert head_file(case, f"{FOLDER}/MEMORY.md") == b"Old lesson.\nNew lesson.\n"
    assert origin_file(case, "README.md") == b"Project.\n"


def test_a_tick_whose_base_fetch_fails_still_commits_role_memory_first(case, tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from foragent.scheduler import TickContext, tick
    from foragent.state import StateStore
    from foragent.tracker import FakeTracker

    now = datetime(2026, 11, 2, 12, tzinfo=UTC)
    monkeypatch.setattr("foragent.scheduler.machine_load", lambda: ("normal", 0.0))
    context = TickContext(repo="owner/repo", repo_root=case.root, tracker=FakeTracker(strict=True),
                          store=StateStore(tmp_path / "state", now=lambda: now), config=case.ctx.config,
                          git=case.ctx.git, now=lambda: now, start_runs=False,
                          launcher=lambda *a, **kw: pytest.fail("No lane job is due"))
    (case.root / FOLDER / "새-교훈.md").write_text("Learned offline.\n")
    case.offline.append(True)
    with pytest.raises(GitError, match="offline"):
        tick(context)  # The base fetch still fails the tick, after the role memory commit.
    assert head_file(case, f"{FOLDER}/새-교훈.md") == b"Learned offline.\n"
    case.offline.clear()
    assert "role memory published to main" in tick(context).actions
    assert origin_file(case, f"{FOLDER}/새-교훈.md") == b"Learned offline.\n"


@pytest.mark.parametrize("name", ["carriage\rreturn.md", "line\nbreak.md", "tab\tand \"quote\".md"])
def test_any_byte_in_a_memory_file_name_reaches_the_commit_and_the_retry(case, name):
    path = f"{FOLDER}/{name}"
    (case.root / path).write_text("A lesson with an unusual name.\n")
    case.offline.append(True)
    assert "delivery failed" in " ".join(publish(case))
    assert head_file(case, path) == b"A lesson with an unusual name.\n"
    case.offline.clear()
    assert publish(case) == ["role memory published to main"]
    assert origin_file(case, path) == b"A lesson with an unusual name.\n"
    assert publish(case) == []


def at(case, minutes: int) -> list[str]:
    case.clock.value = NOW + timedelta(minutes=minutes)
    return publish(case)


def pending(case) -> str | None:
    result = subprocess.run(["git", "rev-parse", "--verify", "--quiet", orch_role_memory.PENDING],
                            cwd=case.root, capture_output=True, text=True)
    return result.stdout.strip() or None


def test_a_write_inside_the_window_is_held_off_the_base_and_out_of_the_index(case):
    memory = case.root / FOLDER / "MEMORY.md"
    memory.write_text("Old lesson.\nFirst.\n")
    assert at(case, 0) == ["role memory published to main"]
    head = run(case.root, "rev-parse", "HEAD")
    memory.write_text("Old lesson.\nFirst.\nSecond.\n")
    (case.root / FOLDER / "new.md").write_text("New file.\n")
    actions = at(case, 10)
    assert actions == ["role memory held until 2026-11-02T12:30:00+00:00 (one push per 30 minutes)"]
    assert run(case.root, "rev-parse", "HEAD") == head == run(case.origin, "rev-parse", "main")
    assert run(case.root, "diff", "--cached", "--name-only") == "", "A held snapshot must leave nothing staged"
    assert run(case.root, "show", f"{pending(case)}:{FOLDER}/new.md") == "New file.\n"
    assert at(case, 20) == [], "An unchanged held snapshot is not rewritten"
    assert at(case, 30) == ["role memory published to main"]
    assert origin_file(case, f"{FOLDER}/MEMORY.md") == b"Old lesson.\nFirst.\nSecond.\n"
    assert origin_file(case, f"{FOLDER}/new.md") == b"New file.\n"
    assert pending(case) is None


def test_held_lessons_come_back_after_a_cleanup_and_join_a_later_write(case):
    memory = case.root / FOLDER / "MEMORY.md"
    memory.write_text("Old lesson.\nFirst.\n")
    at(case, 0)
    memory.write_text("Old lesson.\nFirst.\nHeld.\n")
    at(case, 5)
    run(case.root, "restore", "--source=HEAD", "--worktree", "--", FOLDER)  # A checkout cleanup.
    at(case, 6)
    assert memory.read_text() == "Old lesson.\nFirst.\nHeld.\n", "Held lessons return to the checkout"
    memory.write_text(memory.read_text() + "Later.\n")
    at(case, 7)
    assert at(case, 31) == ["role memory published to main"]
    assert origin_file(case, f"{FOLDER}/MEMORY.md") == b"Old lesson.\nFirst.\nHeld.\nLater.\n"


def test_the_push_time_is_recorded_only_after_a_successful_push(case):
    record = case.ctx.store.root / orch_role_memory.RECORD
    (case.root / FOLDER / "MEMORY.md").write_text("Old lesson.\nFirst.\n")
    case.offline.append(True)
    assert "delivery failed" in " ".join(at(case, 0))
    assert not record.exists(), "A failed delivery must not open a window"
    case.offline.clear()
    assert at(case, 1) == ["role memory published to main"]
    assert json.loads(record.read_text()) == {"pushed_at": "2026-11-02T12:01:00+00:00"}
    (case.root / FOLDER / "MEMORY.md").write_text("Old lesson.\nFirst.\nSecond.\n")
    assert "held until 2026-11-02T12:31:00" in " ".join(at(case, 30))
    assert at(case, 31) == ["role memory published to main"]


def test_a_write_right_after_a_cleanup_keeps_both_held_and_new_lessons(case):
    memory = case.root / FOLDER / "MEMORY.md"
    memory.write_text("Old lesson.\nFirst.\n")
    at(case, 0)
    memory.write_text("Old lesson.\nFirst.\nHeld.\n")
    at(case, 5)
    run(case.root, "restore", "--source=HEAD", "--worktree", "--", FOLDER)  # A cleanup, then a write
    memory.write_text("Old lesson.\nFirst.\nLater.\n")                       # before any tick sees it.
    actions = at(case, 8)
    assert not any("conflict" in action for action in actions)
    assert memory.read_text() == "Old lesson.\nFirst.\nHeld.\nLater.\n"
    assert at(case, 31) == ["role memory published to main"]
    assert origin_file(case, f"{FOLDER}/MEMORY.md") == b"Old lesson.\nFirst.\nHeld.\nLater.\n"


def two_roles(case) -> str:
    """Add a second role whose directory is already on the base branch."""
    other = ".claude/agent-memory/fa-architect"
    case.history.update({f"{other}/MEMORY.md": "Architect lesson.\n"}, message="Architect memory")
    run(case.root, "fetch", "origin", "main")
    run(case.root, "merge", "--ff-only", "origin/main")
    roles = {"docs": {"agent": "fa-docs", "skills": [], "memory": FOLDER + "/"},
             "architect": {"agent": "fa-architect", "skills": [], "memory": other + "/"}}
    case.ctx.config = Config(repo="owner/repo", language="en", skill_libraries=[], roles=roles)
    return other


def test_a_held_lesson_survives_deleting_its_folder_while_another_role_folder_remains(case):
    import shutil

    two_roles(case)
    memory = case.root / FOLDER / "MEMORY.md"
    memory.write_text("Old lesson.\nFirst.\n")
    at(case, 0)
    memory.write_text("Old lesson.\nFirst.\nHeld.\n")
    (case.root / FOLDER / "new.md").write_text("Held new file.\n")
    at(case, 5)
    shutil.rmtree(case.root / FOLDER)  # A cleanup deletes the whole held folder.
    at(case, 10)
    assert memory.read_text() == "Old lesson.\nFirst.\nHeld.\n", "The held folder returns to the checkout"
    shutil.rmtree(case.root / FOLDER)  # Deleted again just before the window closes.
    assert at(case, 31) == ["role memory published to main"]
    assert origin_file(case, f"{FOLDER}/MEMORY.md") == b"Old lesson.\nFirst.\nHeld.\n"
    assert origin_file(case, f"{FOLDER}/new.md") == b"Held new file.\n"
    assert pending(case) is None


def test_held_lessons_are_published_even_when_no_role_folder_is_left(case):
    import shutil

    memory = case.root / FOLDER / "MEMORY.md"
    memory.write_text("Old lesson.\nFirst.\n")
    at(case, 0)
    memory.write_text("Old lesson.\nFirst.\nHeld.\n")
    at(case, 5)
    shutil.rmtree(case.root / FOLDER)
    assert at(case, 31) == ["role memory published to main"], "Publication must not stall"
    assert origin_file(case, f"{FOLDER}/MEMORY.md") == b"Old lesson.\nFirst.\nHeld.\n"
    assert at(case, 40) == []


def origin_versions(case, path: str) -> list[bytes]:
    commits = run(case.origin, "log", "--format=%H", "main", "--", path).split()
    return [subprocess.run(["git", "show", f"{commit}:{path}"], cwd=case.origin, capture_output=True).stdout
            for commit in commits]


def binary_case(case, later: bytes) -> tuple[str, Path]:
    path = f"{FOLDER}/binary.md"
    lesson = case.root / path
    lesson.write_bytes(b"\0Old.\n")
    at(case, 0)
    lesson.write_bytes(b"\0Old.\nHeld.\n")
    at(case, 5)
    lesson.write_bytes(later)
    return path, lesson


def test_a_binary_lesson_edited_twice_in_the_window_waits_and_keeps_both_versions(case):
    path, lesson = binary_case(case, b"\0Old.\nHeld.\nLater.\n")
    head = run(case.origin, "rev-parse", "main")
    assert all("published" not in action for action in at(case, 8)), "The window holds a binary edit too"
    assert run(case.origin, "rev-parse", "main") == head
    assert lesson.read_bytes() == b"\0Old.\nHeld.\nLater.\n", "The checkout must never be overwritten"
    assert at(case, 31) == ["role memory published to main"]
    assert origin_file(case, path) == b"\0Old.\nHeld.\nLater.\n"
    assert pending(case) is None


@pytest.mark.parametrize("moved", [False, True])
def test_a_binary_lesson_rewritten_after_a_cleanup_keeps_the_held_version_in_history(case, moved):
    path, lesson = binary_case(case, b"\0Old.\nLater.\n")  # A cleanup reset it, then a role wrote.
    at(case, 8)
    assert lesson.read_bytes() == b"\0Old.\nLater.\n", "The checkout keeps the latest write"
    if moved:
        case.history.tip = run(case.origin, "rev-parse", "main").strip()  # Origin moved with our pushes.
        case.history.update({"OTHER.md": "Merged elsewhere.\n"}, message="Moved")
    assert at(case, 31) == ["role memory published to main"]
    assert origin_file(case, path) == b"\0Old.\nLater.\n"
    assert b"\0Old.\nHeld.\n" in origin_versions(case, path), "The held version must reach history"
    if moved:
        assert origin_file(case, "OTHER.md") == b"Merged elsewhere.\n"


def test_a_failed_local_commit_leaves_the_newest_lessons_in_the_checkout(case):
    path, lesson = binary_case(case, b"\0Old.\nLater.\n")
    case.commit_failures.append(True)
    assert "not published" in " ".join(at(case, 31))
    assert lesson.read_bytes() == b"\0Old.\nLater.\n", "A failed commit must not leave an older version"
    assert at(case, 32) == ["role memory published to main"]
    assert origin_file(case, path) == b"\0Old.\nLater.\n"
    assert b"\0Old.\nHeld.\n" in origin_versions(case, path)


def test_a_held_lesson_in_a_new_folder_survives_emptying_that_folder(case):
    other = ".claude/agent-memory/fa-new"
    roles = {"docs": {"agent": "fa-docs", "skills": [], "memory": FOLDER + "/"},
             "new": {"agent": "fa-new", "skills": [], "memory": other + "/"}}
    case.ctx.config = Config(repo="owner/repo", language="en", skill_libraries=[], roles=roles)
    (case.root / FOLDER / "MEMORY.md").write_text("Old lesson.\nFirst.\n")
    at(case, 0)
    (case.root / other).mkdir(parents=True)
    (case.root / other / "MEMORY.md").write_text("New role lesson.\n")
    at(case, 5)
    (case.root / other / "MEMORY.md").unlink()  # The directory stays, empty.
    assert all("not published" not in action for action in at(case, 10))
    assert at(case, 31) == ["role memory published to main"]
    assert origin_file(case, f"{other}/MEMORY.md") == b"New role lesson.\n"
