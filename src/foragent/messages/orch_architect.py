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
    },
}
