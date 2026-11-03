"""Gate notice for a token missing the GitHub Projects scope."""

MESSAGES = {
    "en": {
        "board_scope_title": "Board updates",
        "board_scope_fix": "The gh token lacks the project scope. Run: gh auth refresh -h github.com -s project",
    },
    "ko": {
        "board_scope_title": "보드 업데이트",
        "board_scope_fix": "gh 토큰에 project 권한이 없어요. 실행하세요: gh auth refresh -h github.com -s project",
    },
}
