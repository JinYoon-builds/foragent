"""Helpers for the focused issue dashboard width tests (n81-issue-view-width).

One recorded world with every long thing the focused view (`fa dashboard <N>`) prints: long
titles with Korean, emoji and other wide characters, lanes whose titles and path lists are
wide, a dependency wait with a long authored `## Waiting` section, a long held-run detail,
long reviewer verdicts carrying ANSI colour codes, an open PR, and two closed issues (one
merged through its PR, one closed without a merge). Only GitHub and the clock are fake; the
view runs through the real CLI.

Width is measured in terminal cells after removing ANSI (CSI) and OSC 8 hyperlink
decoration: wide (W/F) characters count two, combining marks zero, and a narrow character
followed by VARIATION SELECTOR-16 is drawn two cells wide.
"""

from datetime import timedelta
import re
import unicodedata

REPO = "JinYoon-builds/foragent"
RUNNING = 1     # live explanation: lane paths, issue link, open PR, review text
QUEUED = 4      # live explanation: waits for #1, long authored Waiting section
REQUEUED = 5    # finished-run branch: held run with a long detail, queued again
MERGED = 6      # closed, merged through its PR
CLOSED = 7      # closed without a merge (its PR was closed unmerged)

TITLES = {
    "en": "🛠️ Show the focused issue dashboard ✨ in a narrow 全角 Orca pane without overflowing any line",
    "ko": "🛠️ 좁은 오르카 창에서도 ✨ 이슈 하나만 보는 대시보드가 모든 줄을 창 폭 안에 맞춰 보여줘요",
}
CLOSED_TITLES = {
    "en": "📦 Retire the legacy 全角 importer path after the release notes are published everywhere",
    "ko": "📦 릴리스 노트를 모든 곳에 올린 뒤 예전 가져오기 경로를 정리해요 그리고 문서도 정리해요",
}
# Starts with a token the tests look for; wide characters in both languages.
DETAIL = {
    "en": "DETAIL-7 The review kept failing on the same two findings in 「문서」 paths, "
          "so the run stopped and waits for a smaller plan from the person.",
    "ko": "DETAIL-7 같은 지적 두 개로 리뷰가 계속 실패해서 실행을 멈췄고 더 작은 계획을 기다리고 있어요.",
}
MERGED_DETAIL = {
    "en": "DETAIL-8 Handed to the merge owner after every 全角 check passed on the final head.",
    "ko": "DETAIL-8 마지막 커밋에서 모든 검사가 통과해서 병합 담당에게 넘겼어요.",
}
CLOSED_DETAIL = {
    "en": "DETAIL-9 The PR stayed open for review until the person closed the 全角 issue by hand.",
    "ko": "DETAIL-9 사람이 이슈를 직접 닫을 때까지 PR이 검토를 기다리며 열려 있었어요.",
}
WAITING = {
    "en": ("Reason: the 「문서」 lane must finish the shared path rename in #1 before this can start safely\n"
           "Next condition: #1 merges and its renamed watch_sections paths are on main for every lane\n"
           "Responsible role: orchestrator watching the dependency and the shared file claims"),
    "ko": ("이유: 문서 레인이 #1의 공유 경로 이름 바꾸기를 먼저 끝내야 이 작업을 안전하게 시작할 수 있어요\n"
           "다음 조건: #1이 병합되고 바뀐 watch_sections 경로가 모든 레인의 main에 올라와야 해요\n"
           "담당 역할: 의존성과 공유 파일 점유를 지켜보는 오케스트레이터"),
}
# A reviewer's verdict as recorded, ANSI colour codes included.
VERDICT = ("\x1b[31mREQUEST-CHANGES\x1b[0m: the 「문서」 path list overflows the pane and the "
           "wait detail repeats the 全角 title twice")
# Lane titles and paths with wide characters: the lane/path row is wide in either language.
LANE_TITLES = {"api": "에이피아이 Api", "web": "웹 화면 Web", "docs": "문서 Docs"}
MANY_PATHS = [
    "src/foragent/watch*.py",
    "src/foragent/watch_sections/**",
    "docs/안내서/대시보드/**",
    "src/foragent/status*.py",
    "docs/설계/좁은화면/**",
    "src/foragent/i18n*.py",
]

_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def visible(text: str) -> str:
    """The text a terminal draws: ANSI and OSC 8 hyperlink decoration removed."""
    return _CSI.sub("", _OSC.sub("", text))


def cells(text: str) -> int:
    """Terminal cells of one drawn line (decoration removed)."""
    total = 0
    previous = ""
    for char in visible(text):
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
    """The recorded world described in the module docstring; returns (case, cli main)."""
    from foragent import orch_events
    from foragent.tracker import Dependency
    from test_orch_review_handoff import bind_cli, make_case

    lanes = {slug: {"paths": list(MANY_PATHS), "title": LANE_TITLES[slug], "emoji": "🧩"}
             for slug in ("api", "web", "docs")}
    case = make_case(git_history, tmp_path, monkeypatch, lanes=lanes, language=language, repo=REPO)
    title = TITLES[language]
    body = case.tracker.get_issue(RUNNING).body

    # #1 runs in lane api, with an open PR and a long recorded reviewer verdict.
    case.tracker.update_issue(RUNNING, title=title, body=body)
    case.tracker.add_labels(RUNNING, ["lane:api"])
    case.store.update_run(RUNNING, lane="api", phase="implementing",
                          phase_started_at=(case.clock[0] - timedelta(seconds=120)).isoformat(),
                          last_activity_at=(case.clock[0] - timedelta(seconds=60)).isoformat())
    case.tracker.create_pr(head=f"foragent/issue-{RUNNING}", base="main", title=title,
                           body=f"Closes #{RUNNING}")

    # #4 waits in lane web for #1, with a long authored Waiting section.
    case.tracker.add_issue(QUEUED, title, body + "## Waiting\n" + WAITING[language] + "\n",
                           labels=["approved", "lane:web"])
    case.tracker.set_blocked_by(QUEUED, [Dependency(RUNNING, "open")])

    # #5's run was held with a long detail; it is queued again.
    case.tracker.add_issue(REQUEUED, title, body, labels=["approved", "lane:docs"])
    case.tracker.record_label(REQUEUED, "approved", "human")
    case.store.start_run(REQUEUED, branch=f"foragent/issue-{REQUEUED}",
                         worktree=case.store.worktree_path(REQUEUED), lane="docs")
    case.clock[0] += timedelta(minutes=1)
    case.store.finish_run(REQUEUED, outcome="held", detail=DETAIL[language])

    # #6: its PR was handed to the merge owner, merged, and the issue closed.
    case.tracker.add_issue(MERGED, CLOSED_TITLES[language], body, labels=["approved", "lane:docs"])
    branch = f"foragent/issue-{MERGED}"
    case.store.start_run(MERGED, branch=branch, worktree=case.store.worktree_path(MERGED), lane="docs")
    pr = case.tracker.create_pr(head=branch, base="main", title=CLOSED_TITLES[language],
                                body=f"Closes #{MERGED}")
    case.store.finish_run(MERGED, outcome="merge-queued", detail=MERGED_DETAIL[language])
    case.clock[0] += timedelta(minutes=2)
    case.tracker.merge_pr(pr, expected_head_sha=case.tracker.pr_head_sha(pr))
    case.tracker.close_issue(MERGED, reason="completed")

    # #7: its run left a PR open for review; the person closed the issue without a merge.
    case.tracker.add_issue(CLOSED, CLOSED_TITLES[language], body, labels=["approved", "lane:docs"])
    branch = f"foragent/issue-{CLOSED}"
    case.store.start_run(CLOSED, branch=branch, worktree=case.store.worktree_path(CLOSED), lane="docs")
    case.tracker.create_pr(head=branch, base="main", title=CLOSED_TITLES[language],
                           body=f"Closes #{CLOSED}")
    case.store.finish_run(CLOSED, outcome="pr-open", detail=CLOSED_DETAIL[language])
    case.clock[0] += timedelta(minutes=2)
    case.tracker.close_issue(CLOSED, reason="not_planned")

    # Long reviewer verdicts with ANSI colour codes, as the runner journals them.
    for number in (RUNNING, QUEUED, REQUEUED, MERGED, CLOSED):
        orch_events.append_event(case.store, case.clock[0], number, "review", "reviewer verdict",
                                 round=3, verdict=VERDICT, source="reviewer")
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


def show(main, capsys, monkeypatch, number, columns):
    """`fa dashboard N --once` in a terminal ``columns`` wide: (exit code, stdout, stderr)."""
    monkeypatch.setenv("COLUMNS", str(columns))
    monkeypatch.setenv("LINES", "40")
    capsys.readouterr()
    code = run_cli(main, ["dashboard", str(number), "--once"])
    captured = capsys.readouterr()
    return code, captured.out, captured.err
