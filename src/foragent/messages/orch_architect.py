"""Strings for `fa orch architect`, the orchestrator's on-demand architect session."""

MESSAGES = {
    "en": {
        "architect_help": "Start one architect session that measures and proposes (needs --why)",
        "architect_why_help": "The evidence that made the orchestrator call the architect (fa orch facts numbers or an incident)",
        "architect_why_required": "fa orch architect needs --why with the evidence (fa orch facts numbers or an incident).",
        "architect_running": "An architect session is already running (pid {pid}, started {started_at}); "
                             "wait for its proposals in fa orch inbox.",
        "architect_paused": "The architect cannot start: executor {executor} is paused by a usage limit until {until}.",
        "architect_started": "Architect session started (pid {pid}); proposals arrive in fa orch inbox. Log: {log}",
        "role_help": "Start one session of a standing role from .foragent.yml roles (needs --why)",
        "role_name_help": "The role name, as listed by fa role",
        "role_why_help": "The evidence that made the orchestrator call this role",
        "role_unknown": "No role named {role} in .foragent.yml roles (roles: {roles}); add it with fa role add.",
        "role_why_required": "fa orch role {role} needs --why with the evidence.",
        "role_running": "A {role} session is already running (pid {pid}, started {started_at}); "
                        "wait for its proposals in fa orch inbox.",
        "role_paused": "The {role} role cannot start: executor {executor} is paused by a usage limit until {until}.",
        "role_started": "{role} session started (pid {pid}); proposals arrive in fa orch inbox. Log: {log}",
        "role_short_help": "A one-line reason for people, written by the caller in the config language; stored beside --why",
        "role_short_one_line": "--short must be one line.",
        "settings_desc_scheduled_roles": "Whether the architect, docs role and retrospective wake when an order finishes.",
        "settings_window_label_scheduled_roles": "Scheduled roles",
        "schedule_why_architect": ("Scheduled project review: order {orders} finished, with new merge or run records "
                                   "since {since}. Follow the Project review section of your skill."),
        "schedule_why_role": "Scheduled {role} check: order {orders} finished. Check what that order changed in your area.",
        "schedule_retrospective": ("Retrospective due: order {orders} finished. Review it, append chosen lessons to "
                                   "your memory and record a marked retrospective note."),
    },
    "ko": {
        "architect_help": "측정하고 제안하는 아키텍트 세션 하나를 시작해요 (--why 필요)",
        "architect_why_help": "오케스트레이터가 아키텍트를 부른 근거 (fa orch facts 숫자 또는 실제 사고)",
        "architect_why_required": "fa orch architect에는 근거를 담은 --why가 필요해요 (fa orch facts 숫자 또는 실제 사고).",
        "architect_running": "아키텍트 세션이 이미 실행 중이에요 (pid {pid}, 시작 {started_at}). "
                             "제안은 fa orch inbox에서 기다리세요.",
        "architect_paused": "아키텍트를 시작할 수 없어요: 실행기 {executor}가 사용량 한도로 {until}까지 멈춰 있어요.",
        "architect_started": "아키텍트 세션을 시작했어요 (pid {pid}). 제안은 fa orch inbox에 들어와요. 로그: {log}",
        "role_help": ".foragent.yml roles에 있는 상설 역할의 세션 하나를 시작해요 (--why 필요)",
        "role_name_help": "fa role에 나오는 역할 이름",
        "role_why_help": "오케스트레이터가 이 역할을 부른 근거",
        "role_unknown": ".foragent.yml roles에 {role} 역할이 없어요 (역할: {roles}). fa role add로 추가하세요.",
        "role_why_required": "fa orch role {role}에는 근거를 담은 --why가 필요해요.",
        "role_running": "{role} 세션이 이미 실행 중이에요 (pid {pid}, 시작 {started_at}). "
                        "제안은 fa orch inbox에서 기다리세요.",
        "role_paused": "{role} 역할을 시작할 수 없어요: 실행기 {executor}가 사용량 한도로 {until}까지 멈춰 있어요.",
        "role_started": "{role} 세션을 시작했어요 (pid {pid}). 제안은 fa orch inbox에 들어와요. 로그: {log}",
        "role_short_help": "사람이 읽을 한 줄 이유. 부르는 쪽이 설정 언어로 써요. --why와 함께 저장돼요",
        "role_short_one_line": "--short는 한 줄이어야 해요.",
        "settings_desc_scheduled_roles": "주문이 끝날 때 아키텍트, 문서 역할, 회고를 깨울지 정해요.",
        "settings_window_label_scheduled_roles": "예약된 역할",
        "schedule_why_architect": ("예약된 프로젝트 검토: 주문 {orders}이(가) 끝났고 {since} 뒤로 새 머지나 실행 기록이 있어요. "
                                   "스킬의 Project review 절을 따르세요."),
        "schedule_why_role": "예약된 {role} 점검: 주문 {orders}이(가) 끝났어요. 그 주문이 내 영역에서 바꾼 것을 점검하세요.",
        "schedule_retrospective": ("회고 차례: 주문 {orders}이(가) 끝났어요. 돌아보고, 고른 교훈을 메모리에 덧붙이고, "
                                   "회고 표시가 있는 노트를 남기세요."),
    },
}
