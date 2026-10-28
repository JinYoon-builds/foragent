"""`fa orch stop`: stop a running issue through the stall-kill path and keep its work."""

MESSAGES = {
    "en": {
        "orch_stop_why_required": "fa orch stop: --why must say why the run is stopped; nothing was changed.",
        "orch_stop_not_running": "#{n} is not running; nothing was stopped.",
        "orch_stop_busy": "#{n}: the gate is busy; try again. Nothing was changed.",
        "orch_stop_unsafe": "#{n}: stop refused: the recorded {role} PID {pid} is not a process Foragent "
                            "started for this run ({rule}); nothing was changed.",
        "orch_stop_failed": "#{n}: the run was not stopped: {actions}",
        "orch_stop_done": "#{n}: stopped; unfinished work kept on foragent/issue-{n}. Why: {why}",
    },
    "ko": {
        "orch_stop_why_required": "fa orch stop: --why에 멈추는 이유를 적어 주세요. 아무것도 바꾸지 않았어요.",
        "orch_stop_not_running": "#{n}은 실행 중이 아니에요. 멈춘 것이 없어요.",
        "orch_stop_busy": "#{n}: gate가 바빠요. 다시 시도해 주세요. 아무것도 바꾸지 않았어요.",
        "orch_stop_unsafe": "#{n}: 멈춤을 거절했어요. 기록된 {role} PID {pid}는 Foragent가 이 실행을 위해 "
                            "시작한 프로세스가 아니에요({rule}). 아무것도 바꾸지 않았어요.",
        "orch_stop_failed": "#{n}: 실행을 멈추지 못했어요: {actions}",
        "orch_stop_done": "#{n}: 멈췄어요. 끝내지 못한 작업은 foragent/issue-{n}에 남겼어요. 이유: {why}",
    },
}
