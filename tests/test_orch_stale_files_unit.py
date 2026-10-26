from foragent.orch_stale_files import stale_entries


THEN = ["src/foragent/cli.py", "src/foragent/old_pkg/module.py", "docs/a.md"]
NOW = ["src/foragent/commands/run.py", "docs/a.md"]


def test_moved_path_is_stale_and_new_path_is_not():
    assert stale_entries(["src/foragent/cli.py", "src/foragent/new.py", "docs/a.md"], THEN, NOW) == [
        "src/foragent/cli.py",
    ]


def test_directory_entry_counts_files_below_it():
    assert stale_entries(["src/foragent/old_pkg", "docs"], THEN, NOW) == ["src/foragent/old_pkg"]


def test_glob_with_gone_prefix_or_no_match_is_stale():
    entries = ["src/foragent/old_pkg/**", "src/foragent/*.py", "src/foragent/commands/*.py"]
    assert stale_entries(entries, THEN, NOW) == ["src/foragent/old_pkg/**", "src/foragent/*.py"]


def test_glob_for_a_directory_that_never_existed_is_not_stale():
    assert stale_entries(["src/foragent/planned/**"], THEN, NOW) == []


# Real Git and state below; issue listing is a plain table.
from datetime import UTC, datetime, timedelta
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from foragent import orch_stale_files
from foragent.config import Config
from foragent.git import GitRepo
from foragent.issue import body_hash
from foragent.state import StateStore

T0 = datetime(2026, 10, 20, 8, tzinfo=UTC)
APPROVED_AT = T0 + timedelta(hours=1)


def _git(root, *args, at=None, input=None):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@e")
    if at is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"@{int(at.timestamp())} +0000"
    return subprocess.run(["git", *args], cwd=root, env=env, input=input, check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(root, at, files, *parents):
    """Plumbing-only commit of a flat tree (the test guard forbids porcelain commits)."""
    rows = "".join(f"100644 blob {_git(root, 'hash-object', '-w', '--stdin', input=name)}\t{name}\n"
                   for name in sorted(files))
    tree = _git(root, "mktree", input=rows)
    return _git(root, "commit-tree", tree, *[arg for sha in parents for arg in ("-p", sha)],
                "-m", "c", at=at)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "--bare")
    return root


def _context(tmp_path, root, issues):
    store = StateStore(tmp_path / "state")
    body = "## Files\n- a.py\n"
    for number in issues:
        store.save_approval(number, body=body, body_sha=body_hash(body), acceptance_blobs={},
                            approved_by="human", approved_at=APPROVED_AT.isoformat())
    return SimpleNamespace(store=store, git=GitRepo(root), config=Config(base_branch="main"),
                           now=lambda: APPROVED_AT + timedelta(hours=5))


def _point_base(root, sha):
    _git(root, "update-ref", "refs/remotes/origin/main", sha)


def _events(context):
    path = context.store.root / "orch-events.jsonl"
    lines = path.read_text().splitlines() if path.exists() else []
    return [json.loads(line)["issue"] for line in lines]


def test_topic_commit_merged_after_approval_is_not_the_approval_base(tmp_path, repo):
    # The topic deletes a.py before approval but reaches main only afterwards.
    root = _commit(repo, T0, ["a.py", "b.py"])
    topic = _commit(repo, T0 + timedelta(minutes=30), ["b.py"], root)
    before = _commit(repo, T0 + timedelta(minutes=10), ["a.py", "b.py", "c.py"], root)
    merge = _commit(repo, APPROVED_AT + timedelta(hours=1), ["b.py", "c.py"], before, topic)
    _point_base(repo, merge)
    context = _context(tmp_path, repo, [7])
    assert orch_stale_files.approval_base(context, "origin/main", APPROVED_AT.isoformat()) == before


def test_dedup_survives_close_reopen_and_a_base_returning_to_a_reported_sha(tmp_path, repo, monkeypatch):
    root = _commit(repo, T0, ["a.py", "b.py"])
    first = _commit(repo, APPROVED_AT + timedelta(minutes=1), ["b.py"], root)
    second = _commit(repo, APPROVED_AT + timedelta(minutes=2), ["b.py", "d.py"], first)
    context = _context(tmp_path, repo, [7])
    state = {"state": "open"}
    monkeypatch.setattr(orch_stale_files, "local_issue_models",
                        lambda store: [SimpleNamespace(number=7, state=state["state"])])

    def tick(sha, issue_state="open"):
        state["state"] = issue_state
        _point_base(repo, sha)
        orch_stale_files.report(context)
        return len(_events(context))

    assert tick(first) == 1
    assert tick(first, "closed") == 1
    assert tick(first) == 1  # Reopened at the same SHA.
    assert tick(second) == 2
    assert tick(first) == 2  # The base moved back to an already reported SHA.


@pytest.mark.parametrize("entry", ["**", "**/**", "a.py", "*.py"])
def test_empty_listing_elements_are_not_file_names(entry):
    # A raw split of an empty tree listing is [""]; it must not match "**".
    assert stale_entries([entry], ["a.py"], [""]) == [entry]
    assert stale_entries([entry], ["a.py"], []) == [entry]


def test_deleting_every_file_reports_recursive_globs_on_an_empty_tree(tmp_path, repo, monkeypatch):
    root = _commit(repo, T0, ["a.py"])
    empty = _commit(repo, APPROVED_AT + timedelta(minutes=1), [], root)
    context = _context(tmp_path, repo, [])
    body = "## Files\n- **\n- **/**\n"
    context.store.save_approval(8, body=body, body_sha=body_hash(body), acceptance_blobs={},
                                approved_by="human", approved_at=APPROVED_AT.isoformat())
    monkeypatch.setattr(orch_stale_files, "local_issue_models",
                        lambda store: [SimpleNamespace(number=8, state="open")])
    _point_base(repo, empty)
    orch_stale_files.report(context)
    events = [json.loads(line) for line in (context.store.root / "orch-events.jsonl").read_text().splitlines()]
    assert [event["issue"] for event in events] == [8]
    assert "**, **/**" in events[0]["summary"]


# Every tick exit that holds the lock scans: GitHub waits and rate limits read only local state.
from dataclasses import replace  # noqa: E402

from conftest import BASE_TIME, git_command  # noqa: E402
from foragent import scheduler  # noqa: E402
from foragent.tracker import TrackerError  # noqa: E402
from test_scheduler import BODY, approve, ctx  # noqa: E402,F401


@pytest.mark.parametrize("exit_path", ["wait-before-fetch", "wait-after-refresh", "rate-limit-error"])
def test_github_wait_and_rate_limit_ticks_still_report(ctx, git_history, monkeypatch, exit_path):  # noqa: F811
    ctx.config = replace(ctx.config, skill_libraries=[], test_command="true")
    gone = "src/foragent/gone.py"
    git_history.update({gone: "# old\n"}, at=BASE_TIME + timedelta(minutes=1))
    approve(ctx, 5, body=BODY + f"\n## Files\n- {gone}\n")
    assert scheduler.tick(ctx).skipped is None
    assert ctx.store.load_approval(5) is not None
    git_history.update({gone: None}, at=ctx.now() + timedelta(minutes=1))
    later = ctx.now() + timedelta(minutes=5)
    ctx.now = lambda: later
    if exit_path == "wait-before-fetch":
        git_command(ctx.repo_root, "fetch", "origin")  # An earlier fetch; this tick does not fetch.
        ctx.store.pause_github(later + timedelta(hours=1))
    elif exit_path == "wait-after-refresh":
        flush = ctx.tracker.flush_outbox

        def pause_during_refresh():
            flush()
            ctx.store.pause_github(later + timedelta(hours=1))
        monkeypatch.setattr(ctx.tracker, "flush_outbox", pause_during_refresh)
    else:
        def limited(context, report):
            raise TrackerError("API rate limit exceeded")
        monkeypatch.setattr(scheduler, "_recover_runs", limited)
    report = scheduler.tick(ctx)
    assert report.skipped is not None and "rate limit" in report.skipped
    path = ctx.store.root / "orch-events.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    assert [event["issue"] for event in events if event["kind"] == "stale-files"] == [5]
