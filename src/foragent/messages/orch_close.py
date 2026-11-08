"""Superseded issue closure."""

MESSAGES = {
    "en": {
        "orch_close_help": "Close superseded work and remove its unmerged locked tests",
        "orch_close_arguments": "Issue numbers must be positive and --why must not be empty; nothing was changed.",
        "orch_close_busy": "#{n}: the gate is busy; nothing was changed.",
        "orch_close_running": "#{n} is running; stop it first. Nothing was changed.",
        "orch_close_merged": "#{n} already merged; nothing was changed.",
        "orch_close_issue_required": "#{n} must be an issue; nothing was changed.",
        "orch_close_done": "#{n}: closed. Superseded by {by}: {why}",
        "orch_close_description": (
            "Close an issue whose work was superseded and never merged. This closes the issue "
            "as not planned, closes its open Foragent PR and deletes the PR branch, and removes "
            "its locked test files from the base branch unless a merged issue still uses them."
        ),
        "orch_close_number_help": "the superseded issue to close",
        "orch_close_superseded_by_help": "the issue or issues that replace it (a split may list several)",
        "orch_close_why_help": "the reason, recorded in the closing comment and the journal",
        "orch_close_missing": "Issue #{n} does not exist; nothing was changed.",
    },
    "ko": {
        "orch_close_help": "대체된 작업을 닫고 병합되지 않은 전용 잠금 시험을 제거해요",
        "orch_close_arguments": "이슈 번호는 양수이고 --why에는 이유가 있어야 해요. 아무것도 바꾸지 않았어요.",
        "orch_close_busy": "#{n}: gate가 바빠요. 아무것도 바꾸지 않았어요.",
        "orch_close_running": "#{n}은 실행 중이에요. 먼저 멈춰 주세요. 아무것도 바꾸지 않았어요.",
        "orch_close_merged": "#{n}은 이미 병합됐어요. 아무것도 바꾸지 않았어요.",
        "orch_close_issue_required": "#{n}은 이슈여야 해요. 아무것도 바꾸지 않았어요.",
        "orch_close_done": "#{n}: 닫았어요. {by}에서 대체해요. 이유: {why}",
        "orch_close_description": (
            "대체되어 병합되지 않은 이슈를 닫아요. 이슈를 '계획 없음'으로 닫고, 열린 Foragent PR을 "
            "닫고 PR 브랜치를 지워요. 병합된 이슈가 아직 쓰지 않는 전용 잠금 시험 파일은 base 브랜치에서 지워요."
        ),
        "orch_close_number_help": "닫을 대체된 이슈 번호",
        "orch_close_superseded_by_help": "이 이슈를 대신하는 이슈 번호 (나눠졌다면 여러 개)",
        "orch_close_why_help": "이유. 닫는 댓글과 기록에 남아요",
        "orch_close_missing": "#{n} 이슈가 없어요. 아무것도 바꾸지 않았어요.",
    },
}
