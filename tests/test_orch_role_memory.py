"""Role memory publication: local commit before any remote read, byte-exact paths, moved base."""

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


def run(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def case(git_history):
    """Real Git objects; commit is a path-only fast-import and push a fetch into the bare origin."""
    git_history.update({f"{FOLDER}/MEMORY.md": "Old lesson.\n", "README.md": "Project.\n"})
    root = git_history.clone
    run(root, "fetch", "origin", "main")
    run(root, "merge", "--ff-only", "origin/main")
    offline = []

    def transport(args):
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
    ctx = SimpleNamespace(repo_root=root, config=config, git=GitRepo(root, transport))
    return SimpleNamespace(root=root, origin=git_history.origin, history=git_history, ctx=ctx, offline=offline)


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
