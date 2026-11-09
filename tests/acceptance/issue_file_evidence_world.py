"""World for issue-file-evidence (N88): issue Files versus lane globs, and shared-file waits.

Real CLI, real gate tick, real local state and approvals; only GitHub and the clock are fake.

Lanes own globs only (`work/**`, `web/**` + `assets/**`, `docs/**`). Every issue that
declares Files declares concrete paths that differ from its lane's globs:

- #1 runs in lane work and claims `work/a.py`, `work/b.py`, `work/z.py`.
- #2 (lane web, queued) declares `work/a.py`, `work/b.py` and `web/only_two.py`; it shares
  exactly the first two with #1, the third path only #2 declares.
- #3 (lane docs, queued) declares `docs/three.md`, shared with nobody.
- #4 (lane web, queued) depends on #3 and shares only `work/z.py` with #1, plus its own
  `web/only_four.py`.
- #5 (lane web, queued, control) depends on #1 and shares `work/a.py` with that same #1.
- #6 (lane web, queued) declares no Files at all.
- #7 (lane web, queued) declares the glob `work/**` and `web/seven.py`: a glob is no concrete
  shared path, so nothing may name #1's concrete files as shared with #7.
"""

from datetime import timedelta
import re

RUNNER = 1
SHARER = 2
DEPENDENCY = 3
SPLIT = 4
SAME_PEER = 5
NO_FILES = 6
GLOB = 7

LANE_GLOBS = {"work": ["work/**"], "web": ["web/**", "assets/**"], "docs": ["docs/**"]}
FILES = {
    RUNNER: ["work/a.py", "work/b.py", "work/z.py"],
    SHARER: ["work/a.py", "work/b.py", "web/only_two.py"],
    DEPENDENCY: ["docs/three.md"],
    SPLIT: ["work/z.py", "web/only_four.py"],
    SAME_PEER: ["work/a.py", "web/only_five.py"],
    NO_FILES: None,
    GLOB: ["work/**", "web/seven.py"],
}
LANES = {RUNNER: "work", SHARER: "web", DEPENDENCY: "docs", SPLIT: "web", SAME_PEER: "web", NO_FILES: "web",
         GLOB: "web"}
TITLES = {RUNNER: "Runner work", SHARER: "Shares two files", DEPENDENCY: "Dependency work",
          SPLIT: "Depends on one, overlaps another", SAME_PEER: "Depends on and overlaps one",
          NO_FILES: "Undeclared scope", GLOB: "Declares a glob"}
WIDTH = 240


def body(files):
    text = ("<!-- foragent:v1 -->\n## Goal\nDo the work.\n## Out of scope\nNone.\n"
            "## Done when\n```sh\ntrue\n```\n")
    if files is not None:
        text += "## Files\n" + "".join(f"- {path}\n" for path in files)
    return text


def world(git_history, tmp_path, monkeypatch, *, language="en"):
    from foragent.issue import body_hash
    from foragent.tracker import Dependency
    from test_orch_review_handoff import bind_cli, make_case

    lanes = {slug: {"paths": list(paths), "title": slug.title()} for slug, paths in LANE_GLOBS.items()}
    case = make_case(git_history, tmp_path, monkeypatch, language=language, skill_libraries=[],
                     test_command="true", max_parallel=5, overlap_per_file=1, lanes=lanes)
    approved_at = case.clock[0].isoformat()
    for number, files in FILES.items():
        text = body(files)
        if number == RUNNER:
            case.tracker.update_issue(RUNNER, title=TITLES[RUNNER], body=text)
            case.tracker.add_labels(RUNNER, [f"lane:{LANES[RUNNER]}"])
        else:
            case.tracker.add_issue(number, TITLES[number], text,
                                   labels=["approved", "ready", f"lane:{LANES[number]}"])
        case.store.save_approval(number, body=text, body_sha=body_hash(text), acceptance_blobs={},
                                 approved_by="human", approved_at=approved_at)
    case.store.update_run(RUNNER, lane="work", phase="implementing",
                          phase_started_at=(case.clock[0] - timedelta(seconds=90)).isoformat(),
                          approval_body_sha=body_hash(body(FILES[RUNNER])),
                          declared_files=list(FILES[RUNNER]), effective_files=list(FILES[RUNNER]))
    case.tracker.set_blocked_by(SPLIT, [Dependency(DEPENDENCY, "open")])
    case.tracker.set_blocked_by(SAME_PEER, [Dependency(RUNNER, "open")])
    main = bind_cli(case, monkeypatch)
    tick(case)
    return case, main


def tick(case):
    from foragent.scheduler import tick as gate_tick
    from test_orch_review_handoff import tick_context

    case.clock[0] += timedelta(minutes=10)  # the gate's issue refresh is due
    gate_tick(tick_context(case, start_runs=False))


def run_cli(main, argv):
    try:
        return main(argv)
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else 1


def show(main, capsys, monkeypatch, number, columns=WIDTH):
    """`fa dashboard N --once` in a wide terminal; its plain output."""
    monkeypatch.setenv("COLUMNS", str(columns))
    monkeypatch.setenv("LINES", "60")
    capsys.readouterr()
    code = run_cli(main, ["dashboard", str(number), "--once"])
    captured = capsys.readouterr()
    assert code == 0, f"`fa dashboard {number} --once` must print the issue's view: {captured.err}"
    assert f"#{number}" in captured.out, captured.out
    return _plain(captured.out)


def explanation(case, number, *, width=WIDTH):
    """The main dashboard's explanation bubble after clicking issue N's acorn."""
    from foragent import status, watch
    from test_lane_m2_contract import bubble, pick

    snapshot = watch.collect(status.collect(case.store, case.config, case.clock[0]), case.tracker,
                             case.store, case.config, case.clock[0], repo_root=case.root)
    from foragent.watch import render_frame
    from test_acorn_interact import hit_positions

    state = {"store": case.store}

    def hits():
        layout = {}
        frame = render_frame(snapshot, case.config, case.root, width=width, state=state, layout=layout)
        return hit_positions(layout, frame, width)

    if ("acorn", number) not in hits():
        # Queued work is folded into its lane's '다음 N개' line (#2823): open the list holding it.
        for target in [target for target in hits() if target[0] == "list" and isinstance(target[1], tuple)]:
            pick(snapshot, case.config, case.root, case.store, target, width=width, state=state)
            if ("acorn", number) in hits():
                break
            pick(snapshot, case.config, case.root, case.store, target, width=width, state=state)
    opened, _ = pick(snapshot, case.config, case.root, case.store, ("acorn", number), width=width, state=state)
    return bubble(opened)


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\]8;[^\x07\x1b]*(?:\x07|\x1b\\)")


def _plain(text):
    return ANSI.sub("", text)


def mentions(line, number):
    """The line names issue #number (not #10 for #1)."""
    return re.search(rf"(?<!\d)#{number}(?!\d)", line) is not None
