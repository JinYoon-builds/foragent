"""Standalone local branch review messages."""

MESSAGES = {
    "en": {
        "branch_review_help": "Run the full review panel on your branches and outside contributors' PRs",
        "branch_review_description": "Review a branch you wrote or an outside contributor's PR with the full review panel. "
                                     "Provide the change as a local branch or worktree. "
                                     "The command prints grounded findings without publishing or saving the verdict.",
        "branch_review_target_help": "Local branch or worktree containing your change or a contributor's PR",
        "branch_review_base_help": "Base branch (default: main); review from the merge base",
        "branch_review_goal_help": "Goal to ground findings on",
        "branch_review_issue_help": "Read Goal and Out of scope from this issue",
        "branch_review_unknown": "Unknown local branch or worktree: {target}",
        "branch_review_error": "Review error: {error}",
        "branch_review_cleanup_error": "Cleanup also failed: {error}",
        "branch_review_no_publication": "Standalone reviews cannot publish or change issue state.",
        "branch_review_scope": "Review this local branch change. Report findings without editing files or calling GitHub.",
        "branch_review_head_only": "Reviewing the worktree HEAD commit; uncommitted edits are excluded.",
        "branch_review_verdict": "VERDICT: {verdict}",
        "branch_review_note": "NOTE: {note}",
    },
    "ko": {
        "branch_review_help": "직접 작성한 브랜치와 외부 기여자의 PR을 전체 검토 패널로 검토해요",
        "branch_review_description": "직접 작성한 브랜치나 외부 기여자의 PR을 전체 검토 패널로 검토해요. "
                                     "변경이 담긴 로컬 브랜치나 작업 폴더를 지정하세요. "
                                     "근거가 있는 지적과 판정을 표시하며 결과를 게시하거나 저장하지 않아요.",
        "branch_review_target_help": "직접 작성한 변경이나 외부 기여자의 PR이 담긴 로컬 브랜치 또는 작업 폴더",
        "branch_review_base_help": "기준 브랜치 (기본: main); 공통 조상부터 검토해요",
        "branch_review_goal_help": "지적의 근거로 사용할 목표",
        "branch_review_issue_help": "목표와 범위 제외를 읽을 이슈 번호",
        "branch_review_unknown": "로컬 브랜치나 작업 폴더를 찾을 수 없어요: {target}",
        "branch_review_error": "검토 오류: {error}",
        "branch_review_cleanup_error": "정리 작업도 실패했어요: {error}",
        "branch_review_no_publication": "독립 검토에서는 게시하거나 이슈 상태를 바꿀 수 없어요.",
        "branch_review_scope": "이 로컬 브랜치의 변경을 검토하세요. 파일을 수정하거나 GitHub를 호출하지 말고 지적을 반환하세요.",
        "branch_review_head_only": "작업 폴더의 HEAD 커밋을 검토해요. 커밋하지 않은 변경은 제외돼요.",
        "branch_review_verdict": "VERDICT: {verdict}",
        "branch_review_note": "NOTE: {note}",
    },
}
