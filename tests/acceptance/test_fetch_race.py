"""Acceptance test for fetch-race. Locked by Foragent; do not edit."""

from concurrent.futures import ThreadPoolExecutor
import subprocess
from threading import Barrier

import pytest

from conftest import git_command
from foragent.git import GitError, GitRepo


RACE_ERROR = (
    "error: fetching ref refs/remotes/origin/main failed: "
    "incorrect old value provided\n"
)


@pytest.mark.parametrize("ref", [None, "main"], ids=["default", "explicit-main"])
def test_fetch_race_recovers_after_concurrent_ref_update(git_history, ref):
    calls = []
    git_history.update({"moving.txt": "new remote tip\n"})

    def run(args):
        if args[:1] == ["fetch"]:
            calls.append(args)
            assert len(calls) <= 5, "A ref update race must have a bounded retry budget"
            if len(calls) == 1:
                raise subprocess.CalledProcessError(1, ["git", *args], stderr=RACE_ERROR)
        return git_command(git_history.clone, *args)

    GitRepo(git_history.clone, run).fetch("origin", ref)

    assert 2 <= len(calls) <= 5
    assert git_command(git_history.clone, "rev-parse", "refs/remotes/origin/main") == git_history.tip


def test_fetch_race_repeated_ref_update_errors_are_bounded(git_history):
    calls = []

    def run(args):
        if args[:1] == ["fetch"]:
            calls.append(args)
            assert len(calls) <= 5, "A persistent ref update race must stop within five attempts"
            raise subprocess.CalledProcessError(1, ["git", *args], stderr=RACE_ERROR)
        return git_command(git_history.clone, *args)

    with pytest.raises(GitError) as error:
        GitRepo(git_history.clone, run).fetch()

    assert 1 <= len(calls) <= 5
    assert RACE_ERROR in str(error.value)


def test_fetch_race_other_fetch_errors_raise_once_with_original_text(git_history):
    calls = []
    message = "fatal: could not read from remote repository\n"

    def run(args):
        if args[:1] == ["fetch"]:
            calls.append(args)
            assert len(calls) == 1, "Non-race fetch errors must not be retried"
            raise subprocess.CalledProcessError(128, ["git", *args], stderr=message)
        return git_command(git_history.clone, *args)

    with pytest.raises(GitError) as error:
        GitRepo(git_history.clone, run).fetch()

    assert len(calls) == 1
    assert message in str(error.value)


def test_fetch_race_two_repos_share_a_clone_with_a_moving_local_origin(git_history):
    repos = [GitRepo(git_history.clone), GitRepo(git_history.clone)]

    def fetch_together(repo, start):
        start.wait(timeout=5)
        repo.fetch()

    with ThreadPoolExecutor(max_workers=2) as pool:
        for revision in range(3):
            # Move the real bare origin before each simultaneous pair of fetches.
            git_history.update({"moving.txt": f"revision {revision}\n"})
            start = Barrier(2)
            pending = [pool.submit(fetch_together, repo, start) for repo in repos]
            for future in pending:
                future.result(timeout=10)
            assert git_command(git_history.clone, "rev-parse", "refs/remotes/origin/main") == git_history.tip
