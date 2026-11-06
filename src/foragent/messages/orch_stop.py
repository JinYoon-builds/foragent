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
        "orch_stop_all_busy": "fa orch stop --all: the gate is busy; try again. Nothing was changed.",
        "orch_stop_all_held": "Project stop recorded; no new run starts in lanes {lanes} until you release "
                              "it (fa orch release). Why: {why}",
        "orch_stop_all_owner": "#{n}: saves its unfinished work on foragent/issue-{n} and stops when its "
                               "current step returns.",
        "orch_stop_role_refused": "{role} not started: a project stop is in force ({why}); "
                                  "fa orch release lifts it.",
        "orch_stop_start_refused": "#{n} not started: a project stop is in force ({why}); "
                                   "fa orch release lifts it.",
        "orch_stop_merge_refused": "PR #{n} not merged: a project stop is in force ({why}); "
                                   "fa orch release lifts it.",
        "orch_release_none": "No project stop is in force; nothing was changed.",
        "orch_release_done": "Project stop lifted; lanes resumed: {lanes}. Lanes held before the stop stay held.",
    },
    "ko": {
        "orch_stop_why_required": "fa orch stop: --why에 멈추는 이유를 적어 주세요. 아무것도 바꾸지 않았어요.",
        "orch_stop_not_running": "#{n}은 실행 중이 아니에요. 멈춘 것이 없어요.",
        "orch_stop_busy": "#{n}: gate가 바빠요. 다시 시도해 주세요. 아무것도 바꾸지 않았어요.",
        "orch_stop_unsafe": "#{n}: 멈춤을 거절했어요. 기록된 {role} PID {pid}는 Foragent가 이 실행을 위해 "
                            "시작한 프로세스가 아니에요({rule}). 아무것도 바꾸지 않았어요.",
        "orch_stop_failed": "#{n}: 실행을 멈추지 못했어요: {actions}",
        "orch_stop_done": "#{n}: 멈췄어요. 끝내지 못한 작업은 foragent/issue-{n}에 남겼어요. 이유: {why}",
        "orch_stop_all_busy": "fa orch stop --all: gate가 바빠요. 다시 시도해 주세요. 아무것도 바꾸지 않았어요.",
        "orch_stop_all_held": "프로젝트 정지를 기록했어요. 다시 풀기 전까지 레인 {lanes}에서 새 실행을 "
                              "시작하지 않아요(fa orch release). 이유: {why}",
        "orch_stop_all_owner": "#{n}: 지금 단계가 끝나면 끝내지 못한 작업을 foragent/issue-{n}에 남기고 멈춰요.",
        "orch_stop_role_refused": "{role}을 시작하지 않았어요. 프로젝트 정지가 걸려 있어요({why}). "
                                  "fa orch release로 풀어요.",
        "orch_stop_start_refused": "#{n}을 시작하지 않았어요. 프로젝트 정지가 걸려 있어요({why}). "
                                   "fa orch release로 풀어요.",
        "orch_stop_merge_refused": "PR #{n}을 머지하지 않았어요. 프로젝트 정지가 걸려 있어요({why}). "
                                   "fa orch release로 풀어요.",
        "orch_release_none": "걸려 있는 프로젝트 정지가 없어요. 아무것도 바꾸지 않았어요.",
        "orch_release_done": "프로젝트 정지를 풀었어요. 다시 연 레인: {lanes}. 정지 전부터 보류된 레인은 그대로예요.",
    },
}
