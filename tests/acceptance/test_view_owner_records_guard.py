"""Regression controls for view-reads-owner-records (not in Done when: these pass on main).

They keep the owner-record change honest from the other side: startable work still counts in
the finish estimate, the hero box's waiting sentence (out of scope) still follows `fa gate
status`, and the main screen keeps counting merges the merge owner records, exactly like merges
the worker made itself.
"""

from datetime import timedelta
import json

import pytest

from owner_records_support import (
    HELD_KINDS, admission_startable, admission_world, finish_sentences, gate_queued, next_tick, screen,
    waiting_numbers,
)


@pytest.fixture(autouse=True)
def utf8_terminal(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")


def test_every_startable_issue_counts_in_the_startable_count_and_the_finish_estimate(
        git_history, tmp_path, monkeypatch, capsys):
    # The CLI is bound to the newest world, so each screen is read before the next world exists.
    one, one_main = admission_world(git_history, tmp_path, monkeypatch, name="one")
    one_screen = screen(one, one_main, capsys)
    two, two_main = admission_world(git_history, tmp_path, monkeypatch, startable=(2, 3), name="two")
    found, work = admission_startable(two, two_main, capsys)
    assert found == {2, 3}, json.dumps(work)
    two_screen = screen(two, two_main, capsys)
    assert waiting_numbers(one_screen) == [1], one_screen
    assert waiting_numbers(two_screen) == [2], two_screen
    assert finish_sentences(one_screen) and finish_sentences(two_screen), two_screen
    assert finish_sentences(one_screen) != finish_sentences(two_screen), (
        f"a second startable size:l issue moves the finish estimate:\n{one_screen}\n{two_screen}")


def test_finish_estimate_leaves_out_parked_work(git_history, tmp_path, monkeypatch, capsys):
    """Parked approved work is already outside the finish estimate on main; keep it so."""
    alone, alone_main = admission_world(git_history, tmp_path, monkeypatch, name="alone")
    expected = finish_sentences(screen(alone, alone_main, capsys))
    case, main = admission_world(git_history, tmp_path, monkeypatch, held="parked", name="held")
    shown = screen(case, main, capsys)
    assert expected and finish_sentences(shown) == expected, shown


@pytest.mark.parametrize("held", HELD_KINDS)
def test_hero_waiting_sentence_still_follows_gate_status(git_history, tmp_path, monkeypatch, capsys, held):
    """Out of scope here: the hero box's 'N wait their turn' stays tied to `fa gate status`
    (locked by test_dash_waiting_count.py), even while the bubble's startable count leaves out
    work admission holds."""
    case, main = admission_world(git_history, tmp_path, monkeypatch, held=held, name="held")
    queued = gate_queued(case, main, capsys)
    for width in (80, 160):
        shown = screen(case, main, capsys, width=width)
        found = waiting_numbers(shown)
        assert found == ([queued] if queued else []), f"queued {queued}, found {found}:\n{shown}"


def _merged_world(git_history, tmp_path, monkeypatch, capsys, who):
    """#5 in web: the worker merges it, or the worker leaves it merge-queued and the owner records
    the merge after the last tick; the screen is read after the next tick."""
    from foragent.scheduler import tick
    from test_orch_review_handoff import bind_cli, make_case, tick_context

    folder = tmp_path / who
    folder.mkdir()
    lanes = {"api": {"paths": ["api/**"]}, "web": {"paths": ["web/**"]}}
    case = make_case(git_history, folder, monkeypatch, lanes=lanes, language="en", merge_owner=True)
    main = bind_cli(case, monkeypatch)
    case.tracker.add_labels(1, ["lane:api"])
    case.store.update_run(1, lane="api", phase="implementing")
    now = case.clock[0]
    tick(tick_context(case, start_runs=False))
    case.tracker.add_issue(5, "Merged five", "Body.", labels=["approved", "lane:web", "size:s"])
    case.clock[0] = now - timedelta(minutes=30)
    run = case.store.start_run(5, branch="foragent/issue-5", worktree=case.store.worktree_path(5), lane="web")
    case.clock[0] = now - timedelta(minutes=20)
    case.store.finish_run(5, outcome="merged" if who == "worker" else "merge-queued")
    if who == "owner":
        case.store.record_owner_merge(5, run.started_at, {
            "pr": 1005, "target": "main", "head": "5" * 40, "merged_at": (now - timedelta(minutes=10)).isoformat()})
    case.tracker.close_issue(5)
    case.clock[0] = now
    next_tick(case)
    return screen(case, main, capsys)


def test_main_screen_counts_an_owner_merge_like_a_worker_merge(git_history, tmp_path, monkeypatch, capsys):
    worker = _merged_world(git_history, tmp_path, monkeypatch, capsys, "worker")
    owner = _merged_world(git_history, tmp_path, monkeypatch, capsys, "owner")
    first = lambda text: text.splitlines()[0]  # noqa: E731  (the header: today's merges)
    assert "1" in first(worker) and first(owner) == first(worker), f"{first(worker)!r} vs {first(owner)!r}"
    assert "#5" in owner, f"the bubble names the owner's merge:\n{owner}"


def _merge_card(git_history, tmp_path, monkeypatch, capsys, name, merge=None):
    """The member-card world; ``merge`` adds #14 merged today in api after the last tick, then one
    more gate tick: the card is read from the next snapshot."""
    from member_card_support import at, card_text, dashboard_card, member_world

    folder = tmp_path / name
    folder.mkdir()
    case, main = member_world(git_history, folder, monkeypatch)
    if merge is not None:
        merge(case, 14, at(20, 6), at(20, 6, 30))
    next_tick(case)
    _, _, rows, _ = dashboard_card(case, main, capsys, "api")
    return card_text(rows)


def test_member_card_counts_an_owner_merge_like_a_worker_merge(git_history, tmp_path, monkeypatch, capsys):
    """Main already counts a merge-owner merge in the card's merge count (3 → 4 in this world)."""
    from owner_records_support import owner_merge, worker_merge

    base = _merge_card(git_history, tmp_path, monkeypatch, capsys, "base")
    owner = _merge_card(git_history, tmp_path, monkeypatch, capsys, "owner", owner_merge)
    worker = _merge_card(git_history, tmp_path, monkeypatch, capsys, "worker", worker_merge)
    assert owner != base, f"the owner's merge changes the card:\nbefore:\n{base}\nafter:\n{owner}"
    assert owner == worker, f"owner and worker merges count alike:\nowner:\n{owner}\nworker:\n{worker}"
