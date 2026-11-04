"""Role memory moves the base branch at most once per 30 minutes, in one commit, and never loses
pending lessons (r456). Locked acceptance; Git objects are real, the clock is the gate's clock."""

from dataclasses import replace
from datetime import timedelta
import subprocess

import pytest

from conftest import git_command
from foragent.scheduler import tick
from foragent.state import StateStore
from foragent.tracker import FakeTracker
from test_role_memory_publication import DIRECTORIES, NOW, publication  # noqa: F401


WINDOW = timedelta(minutes=30)


class Clock:
    def __init__(self):
        self.value = NOW

    def __call__(self):
        return self.value


@pytest.fixture
def batched(publication):  # noqa: F811
    case = publication
    case.clock = Clock()
    case.context.now = case.clock
    return case


def lesson(case, role, text):
    """A role session leaves one new lesson in its memory directory, as role sessions do."""
    path = case.root / DIRECTORIES[role] / "MEMORY.md"
    path.write_bytes(path.read_bytes() + f"{text}\n".encode())
    return f"{DIRECTORIES[role]}/MEMORY.md", text


def at(case, minutes):
    case.clock.value = NOW + timedelta(minutes=minutes)
    tick(case.context)


def origin_head(case):
    return git_command(case.origin, "rev-parse", "refs/heads/main")


def new_commits(case, since):
    return git_command(case.origin, "rev-list", "--reverse", f"{since}..refs/heads/main").split()


def shows(repository, commit, lessons):
    for name, text in lessons:
        result = subprocess.run(["git", "-C", str(repository), "show", f"{commit}:{name}"],
                                capture_output=True, text=True)
        if result.returncode != 0 or text not in result.stdout:
            return False
    return True


def assert_nothing_blocks_the_base(case):
    """Pending lessons never sit as an unpushed local commit on the base branch."""
    local = git_command(case.root, "rev-parse", "refs/heads/main")
    assert subprocess.run(["git", "-C", str(case.origin), "cat-file", "-e", f"{local}^{{commit}}"],
                          capture_output=True).returncode == 0, (
        "The local base holds a commit the remote does not have; a later order publish would diverge")
    assert subprocess.run(["git", "-C", str(case.origin), "merge-base", "--is-ancestor", local,
                           "refs/heads/main"], capture_output=True).returncode == 0


def first_push(case):
    first = [lesson(case, "orchestrator", "Lesson at minute zero.")]
    at(case, 0)
    assert shows(case.origin, "refs/heads/main", first), "The first role memory must still go out at once"
    return origin_head(case)


def test_three_role_writes_within_the_window_go_out_as_one_push_and_one_commit(batched):
    case = batched
    start = first_push(case)
    pending = []
    for minute, role in ((5, "architect"), (12, "docs"), (25, "quality")):
        pending.append(lesson(case, role, f"Lesson from {role} at minute {minute}."))
        at(case, minute)
        assert origin_head(case) == start, (
            f"Role memory moved the base again {minute} minutes after the last push")
        assert_nothing_blocks_the_base(case)
    at(case, 31)
    commits = new_commits(case, start)
    assert len(commits) == 1, f"Expected one role memory commit for three roles, got {len(commits)}"
    assert shows(case.origin, commits[0], pending), "Every pending role's lesson must be in the one push"
    assert_nothing_blocks_the_base(case)
    at(case, 40)
    assert new_commits(case, start) == commits, "An idle cycle must not push again"


def test_a_write_after_the_window_is_pushed_and_starts_the_next_window(batched):
    case = batched
    start = first_push(case)
    held = [lesson(case, "architect", "Held inside the first window.")]
    at(case, 10)
    assert origin_head(case) == start, "A write 10 minutes after a push must wait for the window"
    later = [lesson(case, "docs", "Written after the window ended.")]
    at(case, 45)
    commits = new_commits(case, start)
    assert len(commits) == 1, f"The first tick after the window must push once, got {len(commits)} commits"
    assert shows(case.origin, commits[0], held + later)
    second = origin_head(case)
    last = [lesson(case, "quality", "Written 5 minutes after the second push.")]
    at(case, 50)
    at(case, 74)
    assert origin_head(case) == second, "The window counts from the latest push, not a fixed slot"
    at(case, 76)
    commits = new_commits(case, second)
    assert len(commits) == 1 and shows(case.origin, commits[0], last)
    assert_nothing_blocks_the_base(case)


def test_pending_writes_survive_a_restart_and_a_checkout_cleanup(batched):
    case = batched
    start = first_push(case)
    pending = [lesson(case, "architect", "Pending architect lesson."),
               lesson(case, "docs", "Pending docs lesson.")]
    at(case, 5)
    assert origin_head(case) == start, "A write 5 minutes after a push must wait for the window"
    assert_nothing_blocks_the_base(case)
    # Restart: a new gate process with a fresh state store and context, after a checkout cleanup.
    folders = [DIRECTORIES["architect"], DIRECTORIES["docs"]]
    git_command(case.root, "clean", "-fdx", "--", *folders)
    git_command(case.root, "restore", "--source=HEAD", "--staged", "--worktree", "--", *folders)
    store = StateStore(case.store.root, now=case.clock)
    case.context = replace(case.context, store=store, tracker=FakeTracker(strict=True))
    at(case, 31)
    commits = new_commits(case, start)
    assert len(commits) == 1, f"Pending lessons must go out in one push after a restart, got {len(commits)}"
    assert shows(case.origin, commits[0], pending), "A restart must not lose pending role memory"
    assert_nothing_blocks_the_base(case)
