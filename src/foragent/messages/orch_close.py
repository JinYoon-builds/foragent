"""Superseded issue closure."""

MESSAGES = {
    "en": {
        "orch_close_help": "Close superseded work and remove its unmerged locked tests",
        "orch_close_arguments": "Issue numbers must be positive and --why must not be empty; nothing was changed.",
        "orch_close_busy": "#{n}: the gate is busy; nothing was changed.",
        "orch_close_running": "#{n} is running; stop it first. Nothing was changed.",
        "orch_close_merged": "#{n} already merged; nothing was changed.",
        "orch_close_issue_required": "#{n} must be an issue; nothing was changed.",
        "orch_close_done": "#{n}: closed. Superseded by #{by}: {why}",
    },
    "ko": {
        "orch_close_help": "대체된 작업을 닫고 병합되지 않은 전용 잠금 시험을 제거해요",
        "orch_close_arguments": "이슈 번호는 양수이고 --why에는 이유가 있어야 해요. 아무것도 바꾸지 않았어요.",
        "orch_close_busy": "#{n}: gate가 바빠요. 아무것도 바꾸지 않았어요.",
        "orch_close_running": "#{n}은 실행 중이에요. 먼저 멈춰 주세요. 아무것도 바꾸지 않았어요.",
        "orch_close_merged": "#{n}은 이미 병합됐어요. 아무것도 바꾸지 않았어요.",
        "orch_close_issue_required": "#{n}은 이슈여야 해요. 아무것도 바꾸지 않았어요.",
        "orch_close_done": "#{n}: 닫았어요. #{by}에서 대체해요. 이유: {why}",
    },
}
