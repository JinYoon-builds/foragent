"""Strings for the dashboard focused on one issue (`fa dashboard <N>`)."""

MESSAGES = {
    "en": {
        "issue_view_review": "Last review: {verdict} (round {round})",
        "issue_view_review_none": "Last review: none yet",
        "issue_view_ci_running": "CI: running",
        "issue_view_ci_failed": "CI: failed; the lane is trying again",
        "issue_view_ci_passed": "CI: passed",
        "issue_view_ci_none": "CI: not started",
        "issue_view_ci_stopped": "CI: no result; the run stopped while CI was running",
        "issue_view_state": "State: {state}",
        "issue_view_unknown": "Foragent knows nothing about issue #{number} in this project.",
        "issue_view_quit": "q quits",
    },
    "ko": {
        "issue_view_review": "마지막 리뷰: {verdict} ({round}번째)",
        "issue_view_review_none": "마지막 리뷰: 아직 없어요",
        "issue_view_ci_running": "CI: 도는 중이에요",
        "issue_view_ci_failed": "CI: 실패해서 레인이 다시 해 보고 있어요",
        "issue_view_ci_passed": "CI: 통과했어요",
        "issue_view_ci_none": "CI: 아직 시작 전이에요",
        "issue_view_ci_stopped": "CI: 결과 없음, CI가 도는 중에 실행이 멈췄어요",
        "issue_view_state": "상태: {state}",
        "issue_view_unknown": "이 프로젝트에서 Foragent가 #{number} 이슈를 몰라요.",
        "issue_view_quit": "q: 끝내기",
    },
}
