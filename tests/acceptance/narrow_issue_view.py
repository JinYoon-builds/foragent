"""Helpers for the single-issue view width tests (issue-view-width, UX r1212).

A world like the shared status world, but with the lengths a person meets: the real
repository slug (a long issue link), long titles in English and Korean, and lanes that own
many paths. Only GitHub and the clock are fake; the view runs through the real CLI.
"""

from datetime import timedelta
import unicodedata

REPO = "JinYoon-builds/foragent"
RUNNING = 1
QUEUED = 4
REQUEUED = 5  # queued again after a finished (held) run: the #2482 view

LONG_TITLES = {
    "en": "Show the single-issue dashboard in a phone-width Orca pane without cutting text",
    "ko": "휴대폰 폭의 오르카 창에서도 이슈 하나만 보는 대시보드가 글자를 자르지 않고 보여요",
}
LONG_DETAIL = {
    "en": "The review kept failing on the same two findings, so the run stopped and waits for a new plan.",
    "ko": "같은 지적 두 개로 리뷰가 계속 실패해서 실행을 멈췄고 새 계획을 기다리고 있어요.",
}
# A lane that owns many paths: its path list alone is far wider than 40 columns.
MANY_PATHS = [
    "src/foragent/watch*.py",
    "src/foragent/watch_sections/**",
    "src/foragent/status*.py",
    "src/foragent/cli.py",
    "src/foragent/notify*.py",
    "src/foragent/i18n*.py",
]


def cells(text: str) -> int:
    """Terminal columns of one line: wide (W/F) characters two, combining marks zero.

    A narrow character followed by VARIATION SELECTOR-16 is drawn as a two-column emoji.
    """
    total = 0
    previous = ""
    for char in text:
        if char == "️":
            if previous and unicodedata.east_asian_width(previous) not in ("W", "F"):
                total += 1
            continue
        if unicodedata.combining(char) or unicodedata.category(char) in ("Mn", "Me", "Cf"):
            continue
        total += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        previous = char
    return total


def world(git_history, tmp_path, monkeypatch, *, language="en"):
    """#1 runs in lane api; #4 waits in lane web for #1; #5's run was held, now it is queued again.

    All three titles are long, lanes api, web and docs own many paths, and #5's held run
    recorded a long detail.
    """
    from foragent.tracker import Dependency
    from test_orch_review_handoff import bind_cli, make_case

    lanes = {slug: {"paths": list(MANY_PATHS) if slug in ("api", "web", "docs") else [f"{slug}/**"],
                    "title": slug.title(), "emoji": "🧩"}
             for slug in ("api", "review", "decision", "web", "docs")}
    case = make_case(git_history, tmp_path, monkeypatch, lanes=lanes, language=language, repo=REPO)
    title = LONG_TITLES[language]
    case.tracker.update_issue(RUNNING, title=title, body=case.tracker.get_issue(RUNNING).body)
    case.tracker.add_labels(RUNNING, ["lane:api"])
    case.store.update_run(RUNNING, lane="api", phase="implementing",
                          phase_started_at=(case.clock[0] - timedelta(seconds=120)).isoformat(),
                          last_activity_at=(case.clock[0] - timedelta(seconds=60)).isoformat())
    case.tracker.add_issue(2, "Review dispute", labels=["approved", "blocked", "lane:review"])
    case.tracker.add_issue(3, "Choose locale", labels=["approved", "needs-human", "lane:decision"])
    case.tracker.add_issue(QUEUED, title, labels=["approved", "lane:web"])
    case.tracker.set_blocked_by(QUEUED, [Dependency(RUNNING, "open")])
    case.tracker.add_issue(REQUEUED, title, case.tracker.get_issue(RUNNING).body,
                           labels=["approved", "lane:docs"])
    case.tracker.record_label(REQUEUED, "approved", "human")
    case.store.start_run(REQUEUED, branch=f"foragent/issue-{REQUEUED}",
                         worktree=case.store.worktree_path(REQUEUED), lane="docs")
    case.clock[0] += timedelta(minutes=1)
    case.store.finish_run(REQUEUED, outcome="held", detail=LONG_DETAIL[language])
    main = bind_cli(case, monkeypatch)
    tick(case)
    return case, main


def queued_numbers(main, capsys):
    """The issues `fa gate status --json` lists as queued, the list the main dashboard draws."""
    import json

    capsys.readouterr()
    assert run_cli(main, ["gate", "status", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["lanes"]
    found = set()
    for row in rows.values() if isinstance(rows, dict) else rows:
        found |= {item["number"] if isinstance(item, dict) else item for item in row.get("queued") or []}
    return found


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


def show(main, capsys, monkeypatch, number, columns):
    """`fa dashboard N --once` in a terminal ``columns`` wide; its plain output."""
    monkeypatch.setenv("COLUMNS", str(columns))
    monkeypatch.setenv("LINES", "40")
    capsys.readouterr()
    code = run_cli(main, ["dashboard", str(number), "--once"])
    captured = capsys.readouterr()
    assert code == 0, f"`fa dashboard {number} --once` must print the issue's view: {captured.err}"
    assert f"#{number}" in captured.out, captured.out
    return captured.out
