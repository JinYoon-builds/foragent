"""Acceptance (owner-train-pause, statements 1-2).

PM q73 (2026-11-07): each train merge moved main and the next train was built
before pending publications finished, so no publication landed for hours. The
only lever was editing merge_batch in the shared .foragent.yml. After this
issue `fa orch` pauses the merge owner's train building with a reason and
resumes it:

    fa orch pause-trains --why TEXT [--until ISO]
    fa orch resume-trains

While paused the owner finishes the running train, builds no new train, keeps
applying order publications and main-write requests, and `fa gate status` names the pause and its
reason. After resume the owner builds trains again in the next pass.

Real gate tick, merge owner, merge train, main-write queue, Git and state of the
locked merge-path world (tests/test_merge_owner_batch.py `batched`); only
GitHub, models and the clock are fake. The commands run through the public CLI
(foragent.cli.main) against the same state store the owner reads.
"""

import base64
from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "src/foragent").is_dir())
for directory in (ROOT / "tests", ROOT / "tests/acceptance"):
    if str(directory) not in sys.path:
        sys.path.append(str(directory))

from conftest import git_command  # noqa: E402
from acceptance.test_merge_path_real_settings import world  # noqa: E402, F401
from test_merge_owner_proto import proto  # noqa: E402, F401
import test_merge_owner_proto as support  # noqa: E402
from test_merge_owner_batch import batched, groups, queue_workers, refs  # noqa: E402, F401
from test_order import offline_publication  # noqa: E402, F401  (fixture)
from test_order_publications import case, submit  # noqa: E402, F401  (fixture)
from foragent import main_write_requests, merge_owner, order_publications  # noqa: E402
from foragent.scheduler import TickReport  # noqa: E402

FIRST = [5, 1]
LATER = [4, 2]
REASON = "Let queued order publications land first (PM q73)"
SPEC = "docs/specs/2026-11-07-paused-publication.md"


# ---------------------------------------------------------------- helpers

def bind_cli(case, monkeypatch):
    """Public CLI on the case's repo, config, fake GitHub and the store the owner reads."""
    # support.gate replaces case.store each pass; always hand out the current one.
    return bind(monkeypatch, lambda: case.ctx.config, case.tracker, case.git, lambda: case.store,
                lambda: case.clock[0])


def bind(monkeypatch, config, tracker, git, store, now):
    from datetime import datetime
    from foragent import cli
    from foragent.launchd import LaunchdStatus
    from foragent.state import StateStore

    monkeypatch.setattr(cli, "load_config", lambda root, *a, **kw: config())
    monkeypatch.setattr(cli, "GhTracker", lambda repo: tracker)
    monkeypatch.setattr(cli, "GitRepo", lambda root: git)
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo, **kw: store()))
    monkeypatch.setattr(cli.launchd, "status", lambda *a, **kw: LaunchdStatus("offline", False, False))
    monkeypatch.setattr(cli.gates, "collect", lambda *a, **kw: [])
    monkeypatch.setattr(cli, "kick_tick", lambda *a, **kw: None, raising=False)
    monkeypatch.setenv("FORAGENT_VIEW", "off")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now() if tz else now().replace(tzinfo=None)

    monkeypatch.setattr(cli, "datetime", Clock)
    return cli.main


def fa(case, main, capsys, *argv):
    capsys.readouterr()
    try:
        code = main([*argv, "--repo-root", str(case.root)])
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code in (0, None), captured.out + captured.err


def pause(case, main, capsys, reason=REASON):
    ok, out = fa(case, main, capsys, "orch", "pause-trains", "--why", reason)
    assert ok, f"`fa orch pause-trains --why ...` pauses the merge owner's train building; it failed:\n{out}"
    return out


def resume(case, main, capsys):
    ok, out = fa(case, main, capsys, "orch", "resume-trains")
    assert ok, f"`fa orch resume-trains` resumes the merge owner's train building; it failed:\n{out}"
    return out


def gate_status(case, main, capsys):
    ok, plain = fa(case, main, capsys, "gate", "status")
    assert ok, f"fa gate status failed:\n{plain}"
    ok, raw = fa(case, main, capsys, "gate", "status", "--json")
    assert ok, f"fa gate status --json failed:\n{raw}"
    return plain, json.loads(raw)


def blob(text):
    return {"mode": "100644", "bytes": base64.b64encode(text.encode()).decode("ascii")}


def main_text(case, path):
    try:
        return git_command(case.history.origin, "show", f"main:{path}")
    except Exception:
        return None


def main_tip(case):
    return git_command(case.history.origin, "rev-parse", "refs/heads/main")


def submit_write(case, title, changes):
    files = {}
    for path, text in changes.items():
        current = main_text(case, path)
        files[path] = {"before": None if current is None else blob(current + "\n"), "after": blob(text)}
    return main_write_requests.submit(case.store, "main", title, "pm", {
        "kind": "files", "source": str(case.root), "base": main_tip(case), "files": files})


def write_status(case, row):
    return next(r for r in main_write_requests.requests(case.store) if r["id"] == row["id"])["status"]


def members_landed(case):
    return {n for n, _ in case.store.owner_merge_facts()}


def waiting_train(case, members=FIRST):
    """A train is built and published; its CI is pending."""
    case.pending_batch = True
    queue_workers(case, members)
    support.gate(case)
    assert len(groups(case)) == 1 and refs(case), "the train was not built"
    assert not members_landed(case), "the train must still wait for its CI"
    return groups(case)[0]


def train_members(case, pr):
    from foragent import merge_owner_batch as batch

    _, chain = batch._decode(case.git, pr.head, case.tracker.pr_head_sha(pr.number))
    return [n for n, *_ in chain]


def green_train(case, pr):
    """Only the train's tip CI passes; the members' own PR checks stay pending (no single-path merges)."""
    case.pending_batch = False
    case.tracker.set_pr_checks(pr.number, "pass")


def passes_with_pending_trains(case, count=4):
    """Owner passes in which any new train's CI stays pending, so a built train stays visible."""
    for _ in range(count):
        support.gate(case)
        case.pending_batch = True


# ---------------------------------------------------------------- statement 1

def test_paused_owner_finishes_the_running_train_builds_no_new_one_and_lands_writes(batched, monkeypatch, capsys):
    case = batched
    main = bind_cli(case, monkeypatch)
    first = waiting_train(case)
    tip = case.tracker.pr_head_sha(first.number)

    pause(case, main, capsys)
    queue_workers(case, LATER)  # Work that would form the next train.
    spec = "# Paused publication\nLands while trains are paused.\n"
    row = submit_write(case, "Publish order paused-publication", {SPEC: spec})
    support.gate(case)

    assert refs(case) and len(groups(case)) == 1, (
        "The pause does not retire the running train (unlike turning merge_batch off)")
    assert case.tracker.get_pr(first.number).state == "open", "The running train's PR stays open"
    assert case.tracker.pr_head_sha(first.number) == tip, "The running train is not rebuilt"

    green_train(case, first)
    passes_with_pending_trains(case)

    assert members_landed(case) >= set(FIRST), "The running train finishes and lands while trains are paused"
    assert len(groups(case)) == 1, (
        f"While paused the owner builds no new train after the running one lands; trains: "
        f"{[(p.number, p.head) for p in groups(case)]}")
    assert not refs(case) or all(set(train_members(case, p)) <= set(FIRST) for p in groups(case))
    assert write_status(case, row) == "done", (
        "A main-write request queued during the paused owner's running train lands while trains stay paused")
    assert main_text(case, SPEC) == spec.rstrip("\n")


def test_paused_owner_keeps_applying_main_write_requests(batched, monkeypatch, capsys):
    case = batched
    main = bind_cli(case, monkeypatch)
    pause(case, main, capsys)
    case.pending_batch = True
    queue_workers(case, FIRST)
    before = main_tip(case)
    spec = "# Paused publication\nLands while trains are paused.\n"
    row = submit_write(case, "Publish order paused-publication", {SPEC: spec})
    support.gate(case)
    assert not groups(case), "While paused no train is built"
    assert write_status(case, row) == "done", "The paused owner applies a pending main-write request in its pass"
    assert main_text(case, SPEC) == spec.rstrip("\n")
    assert git_command(case.history.origin, "rev-list", "--count", f"{before}..main") == "1", (
        "The write moves main by exactly one commit")


def test_paused_owner_builds_no_train_from_queued_work(batched, monkeypatch, capsys):
    case = batched
    main = bind_cli(case, monkeypatch)
    pause(case, main, capsys)
    case.pending_batch = True
    queue_workers(case, FIRST)
    passes_with_pending_trains(case, 3)
    assert not groups(case) and not refs(case), "While paused the owner builds no new train"


def test_gate_status_names_the_pause_and_its_reason(batched, monkeypatch, capsys):
    case = batched
    main = bind_cli(case, monkeypatch)
    plain, data = gate_status(case, main, capsys)
    assert REASON not in plain and REASON not in json.dumps(data, ensure_ascii=False)

    pause(case, main, capsys)
    support.gate(case)
    plain, data = gate_status(case, main, capsys)
    assert REASON in plain, f"fa gate status names the pause reason:\n{plain}"
    lines = [line for line in plain.splitlines() if REASON in line]
    assert any("pause" in line.lower() and "train" in line.lower() for line in lines), (
        f"fa gate status says train building is paused, on the reason's line:\n{plain}")
    assert REASON in json.dumps(data, ensure_ascii=False), "fa gate status --json carries the pause reason"

    resume(case, main, capsys)
    support.gate(case)
    plain, data = gate_status(case, main, capsys)
    assert REASON not in plain and REASON not in json.dumps(data, ensure_ascii=False), (
        "After resume fa gate status no longer names the pause")


def test_gate_status_shows_the_latest_reason_once(batched, monkeypatch, capsys):
    case = batched
    main = bind_cli(case, monkeypatch)
    pause(case, main, capsys, "First reason for the pause")
    pause(case, main, capsys, REASON)
    plain, _ = gate_status(case, main, capsys)
    assert plain.count(REASON) == 1, f"fa gate status names the current pause reason once:\n{plain}"


@pytest.fixture
def publishing(case, offline_publication, monkeypatch):
    """A queued order publication on a real local origin; merge_batch is on (tests/acceptance/test_one_commit_per_pass.py)."""
    root, _config, _tracker, git, calls, origin = offline_publication
    case.config = replace(case.config, merge_batch=True)
    case.ctx.config, case.ctx.git, case.ctx.repo_root = case.config, git, root
    main = bind(monkeypatch, lambda: case.ctx.config, case.tracker, git, lambda: case.store,
                lambda: datetime.now(UTC))
    return SimpleNamespace(case=case, ctx=case.ctx, root=root, git=git, origin=origin, store=case.store, main=main)


def test_paused_owner_still_applies_a_pending_order_publication(publishing, capsys):
    o = publishing
    pause(o, o.main, capsys)
    submit(o.case)
    order_publications.check_pending(o.ctx, TickReport())
    row, = order_publications.pending(o.store)
    before = git_command(o.origin, "rev-parse", "main")

    merge_owner.tick(o.ctx, TickReport())

    landed = next(r for r in order_publications.requests(o.store) if r["id"] == row["id"])
    assert landed["status"] == "published", (
        f"The paused owner applies a pending order publication in its pass; status {landed['status']!r}")
    names = git_command(o.origin, "ls-tree", "-r", "--name-only", "main").splitlines()
    for name, data in row["files"].items():
        assert name in names and git_command(o.origin, "show", f"main:{name}") == data.rstrip("\n"), (
            f"The publication's file {name} is on main")
    assert git_command(o.origin, "rev-list", "--count", f"{before}..main") == "1", (
        "The publication moves main by exactly one commit")
    plain, data = gate_status(o, o.main, capsys)
    assert REASON in plain, f"fa gate status still names the pause after the publication landed:\n{plain}"


# ---------------------------------------------------------------- statement 2

def test_resume_builds_trains_again_in_the_next_pass(batched, monkeypatch, capsys):
    case = batched
    main = bind_cli(case, monkeypatch)
    pause(case, main, capsys)
    case.pending_batch = True
    queue_workers(case, FIRST)
    passes_with_pending_trains(case, 2)
    assert not groups(case), "While paused no train is built"

    resume(case, main, capsys)
    support.gate(case)
    assert len(groups(case)) == 1 and refs(case), "After resume the owner builds a train in the next pass"
    assert sorted(train_members(case, groups(case)[0])) == sorted(FIRST), "The train holds the queued work"


def test_one_resume_ends_a_repeated_pause(batched, monkeypatch, capsys):
    case = batched
    main = bind_cli(case, monkeypatch)
    pause(case, main, capsys, "First reason for the pause")
    pause(case, main, capsys)
    case.pending_batch = True
    queue_workers(case, FIRST)
    support.gate(case)
    assert not groups(case)
    resume(case, main, capsys)
    support.gate(case)
    assert len(groups(case)) == 1, "One resume returns to normal batching even after two pauses"


def test_resume_after_a_paused_train_landed_builds_the_next_train(batched, monkeypatch, capsys):
    case = batched
    main = bind_cli(case, monkeypatch)
    first = waiting_train(case)
    pause(case, main, capsys)
    queue_workers(case, LATER)
    green_train(case, first)
    passes_with_pending_trains(case, 3)
    assert members_landed(case) >= set(FIRST) and len(groups(case)) == 1

    resume(case, main, capsys)
    case.pending_batch = True
    support.gate(case)
    later = groups(case)[1:]
    assert len(later) == 1, "After resume the next pass builds the next train"
    assert sorted(train_members(case, later[0])) == sorted(LATER)
