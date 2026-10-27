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
    },
    "ko": {
        "architect_help": "측정하고 제안하는 아키텍트 세션 하나를 시작합니다 (--why 필요)",
        "architect_why_help": "오케스트레이터가 아키텍트를 부른 근거 (fa orch facts 숫자 또는 실제 사고)",
        "architect_why_required": "fa orch architect에는 근거를 담은 --why가 필요합니다 (fa orch facts 숫자 또는 실제 사고).",
        "architect_running": "아키텍트 세션이 이미 실행 중입니다 (pid {pid}, 시작 {started_at}). "
                             "제안은 fa orch inbox에서 기다리세요.",
        "architect_paused": "아키텍트를 시작할 수 없습니다: 실행기 {executor}가 사용량 한도로 {until}까지 멈춰 있습니다.",
        "architect_started": "아키텍트 세션을 시작했습니다 (pid {pid}). 제안은 fa orch inbox에 들어옵니다. 로그: {log}",
    },
}
