"""Shared worlds and readers for the view-reads-owner-records acceptance tests (not a test module).

Every world is the shared offline case (fake tracker, local state store, fake clock) after real
gate ticks, read through the public `fa gate watch --once` screen, `fa orch facts --json` and the
squirrel's member card (`member_card_support.dashboard_card`). Nothing here starts a session,
calls GitHub or sends a signal.
"""

from datetime import timedelta
import json
import re

LANES = {"api": {"paths": ["api/**"]}, "web": {"paths": ["web/**"]}, "ops": {"paths": ["ops/**"]}}
# A lane starts one issue at a time, so each approved issue gets its own free lane.
LANE_OF = {2: "web", 3: "ops"}
ORDER = 900
STARTABLE = 2
HELD = 3
DEPENDENCY = 4
HELD_KINDS = ("dependency", "no_machine_check", "parked", "blocked_label")
NO_CHECK_BODY = ("<!-- foragent:v1 -->\n## Goal\nTidy the ops copy.\n## Out of scope\nNothing else.\n"
                 "## Files\n- ops/page_3.txt\n## Done when\n- [ ] A person reads the page\n")


def _body(number):
    """A startable body: its own Files (so no file rule serialises it) and a machine Done when."""
    return ("<!-- foragent:v1 -->\n## Goal\nImplement the feature.\n## Out of scope\nOther features.\n"
            f"## Files\n- {LANE_OF[number]}/feature_{number}.txt\n## Done when\n"
            f"```sh\ntest -f {LANE_OF[number]}/feature_{number}.txt\n```\n")


def _plain(text):
    return re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)


def _approved(case, number, body, labels):
    from foragent.issue import body_hash

    case.tracker.add_issue(number, f"Work {number}", body, labels=labels)
    case.tracker.set_parent(number, ORDER)
    case.store.save_approval(number, body=body, body_sha=body_hash(body), acceptance_blobs={},
                             approved_by="human", approved_at=case.clock[0].isoformat())


def admission_world(git_history, tmp_path, monkeypatch, *, held=None, startable=(STARTABLE,), name="world",
                    language="en"):
    """One running issue (#1, api, outside the order) and approved size:l work of order #900.

    ``startable`` issues are approved, ready, carry a machine Done when and have an approval record:
    admission records them as startable. ``held`` (one of HELD_KINDS, or None) adds #3, an approved
    issue of the same order and size, in its own free lane (ops), that admission holds:

    - ``dependency``: #3 is blocked by the open issue #4 (`fa gate status` still counts it as queued);
    - ``no_machine_check``: #3's approved Done when has no machine check;
    - ``parked``: the orchestrator parked #3 (`fa orch park`);
    - ``blocked_label``: #3 carries the blocked label.

    Slots are ample (budget 8, ceiling 8), so no issue is held only for a free slot.
    """
    from foragent import capacity
    from foragent.scheduler import tick
    from test_orch_review_handoff import bind_cli, make_case, tick_context

    folder = tmp_path / name
    folder.mkdir()
    case = make_case(git_history, folder, monkeypatch, lanes=LANES, language=language, max_parallel=8)
    capacity.write_state(case.store, 8, case.clock[0])
    main = bind_cli(case, monkeypatch)
    case.tracker.add_labels(1, ["lane:api"])
    case.store.update_run(1, lane="api", phase="implementing")
    case.tracker.add_issue(ORDER, "Order of web work")
    for number in startable:
        _approved(case, number, _body(number), ["approved", "ready", f"lane:{LANE_OF[number]}", "size:l"])
    if held is not None:
        labels = ["approved", "ready", f"lane:{LANE_OF[HELD]}", "size:l"] + (["blocked"] if held == "blocked_label" else [])
        _approved(case, HELD, NO_CHECK_BODY if held == "no_machine_check" else _body(HELD), labels)
        if held == "dependency":
            case.tracker.add_issue(DEPENDENCY, "An open blocker", "Not approved.", labels=[f"lane:{LANE_OF[HELD]}"])
            case.tracker.add_blocked_by(HELD, DEPENDENCY)
    tick(tick_context(case, start_runs=False))
    if held == "parked":
        assert main(["orch", "park", str(HELD), "--why", "Wait for the design",
                     "--repo-root", str(case.root)]) == 0
        tick(tick_context(case, start_runs=False))
    return case, main


def admission_startable(case, main, capsys):
    """Admission's own answer, through `fa orch facts --json`: waiting issues it does not hold."""
    capsys.readouterr()
    assert main(["orch", "facts", "--json", "--repo-root", str(case.root)]) == 0
    payload = json.loads(capsys.readouterr().out)
    found = []

    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get("parallelizable_work"), dict):
                found.append(value["parallelizable_work"])
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    assert found, "fa orch facts --json reports its admission replay (parallelizable_work)"
    work = found[0]
    held = {str(number) for number in work.get("held") or {}}
    return {number for number in work.get("waiting_issues") or [] if str(number) not in held}, work


def gate_queued(case, main, capsys):
    capsys.readouterr()
    assert main(["gate", "status", "--json", "--repo-root", str(case.root)]) == 0
    return json.loads(capsys.readouterr().out)["summary"]["queued"]


def screen(case, main, capsys, width=160):
    import pytest

    capsys.readouterr()
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("COLUMNS", str(width))
        assert main(["gate", "watch", "--once", "--repo-root", str(case.root)]) == 0
    return _plain(capsys.readouterr().out)


def published(case, main, capsys, width=160):
    """Open the dashboard once (it republishes now.json, the bubble's snapshot) and return
    (published summary, printed screen). #2824's contract: the bubble's startable count is
    `summary.startable` of the gate's dashboard snapshot."""
    shown = screen(case, main, capsys, width=width)
    path = case.store.root / "now.json"
    assert path.exists(), "the gate publishes the dashboard snapshot the view reads"
    return json.loads(path.read_text(encoding="utf-8")).get("summary") or {}, shown


def startable_count(summary):
    assert "startable" in summary, (
        f"the bubble's snapshot must carry the startable count (summary.startable): {sorted(summary)}")
    return summary["startable"]


def _flat(text):
    return re.sub(r"\s+", " ", re.sub(r"[│|┤╭╮╰╯─]", " ", text))


def waiting_numbers(text, language="en"):
    """Every 'N wait their turn' number on the screen (the hero box's lane_bubble_queued copy).
    Out of scope for this issue (it follows `fa gate status`); used only by the guard file."""
    from foragent import i18n

    flat, found = _flat(text), []
    one = re.escape(i18n.MESSAGES[language]["lane_bubble_queued_one"]).replace(r"\ ", r"\s*")
    found += [1] * len(re.findall(one, flat))
    head, tail = i18n.MESSAGES[language]["lane_bubble_queued"].split("{count}")
    many = (re.escape(head) + r"(\d+)" + re.escape(tail)).replace(r"\ ", r"\s*")
    found += [int(match) for match in re.findall(many, flat)]
    return found


def finish_sentences(text, language="en"):
    """Every finish-estimate sentence on the screen (lane_bubble_eta / lane_bubble_eta_range copy)."""
    from foragent import i18n

    flat, found = _flat(text), []
    for key in ("lane_bubble_eta_range", "lane_bubble_eta"):
        template = i18n.MESSAGES[language][key]
        pattern = re.escape(template)
        for field in ("low", "high"):
            pattern = pattern.replace(re.escape("{" + field + "}"), r"(.+?)")
        found += [match.group(0) for match in re.finditer(pattern.replace(r"\ ", r"\s*"), flat)]
    return found


# ---------------------------------------------------------------- merge-owner merges on the member card


def owner_merge(case, number, start, end, *, lane="api"):
    """The merge owner's completion of a run the worker left queued: merge-queued, then the owner's record."""
    case.tracker.add_issue(number, f"Owner merged {number}", labels=[f"lane:{lane}"], state="closed")
    saved = case.clock[0]
    case.clock[0] = start
    run = case.store.start_run(number, branch=f"foragent/issue-{number}",
                               worktree=case.store.worktree_path(number), lane=lane)
    case.clock[0] = end - timedelta(minutes=1)
    case.store.finish_run(number, "merge-queued")
    case.store.record_owner_merge(number, run.started_at, {
        "pr": 2000 + number, "target": "main", "head": f"{number:040x}", "merged_at": end.isoformat()})
    case.clock[0] = saved


def worker_merge(case, number, start, end, *, lane="api"):
    """The same merge done by the worker itself (outcome merged), for comparison."""
    case.tracker.add_issue(number, f"Owner merged {number}", labels=[f"lane:{lane}"], state="closed")
    saved = case.clock[0]
    case.clock[0] = start
    case.store.start_run(number, branch=f"foragent/issue-{number}",
                         worktree=case.store.worktree_path(number), lane=lane)
    case.clock[0] = end
    case.store.finish_run(number, "merged")
    case.clock[0] = saved


def next_tick(case):
    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context

    tick(tick_context(case, start_runs=False))
