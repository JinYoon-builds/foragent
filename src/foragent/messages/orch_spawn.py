"""Lines for the gate opening an orchestrator session (D76)."""

MESSAGES = {
    "en": {
        "orch_spawn_title": "Foragent",
        "orch_spawn_opened": "Opened a new orchestrator for {repo} ({screen}).",
        "orch_spawn_tick": "Opened an orchestrator ({screen})",
        "orch_spawn_failed": "Could not open an orchestrator: {error}",
        "orch_ensure_opened": "Opened an orchestrator in the background ({screen}).",
        "orch_ensure_off": "Opened nothing: FORAGENT_ORCH_SPAWN=off.",
        "orch_ensure_alive": "Opened nothing: an orchestrator is already alive.",
        "orch_ensure_no_work": "Opened nothing: there is no approved or running work.",
        "orch_ensure_session": "Opened nothing: an orchestrator session is still running (no fresh heartbeat).",
        "orch_ensure_failed": "Opened nothing: Claude was not found or the session could not start.",
        "orch_ensure_raced": "Opened nothing: another opener just opened or tried to open an orchestrator; check again shortly.",
        "orch_ensure_help": "Open a background orchestrator when none is alive and approved work exists",
    },
    "ko": {
        "orch_spawn_title": "Foragent",
        "orch_spawn_opened": "{repo}의 오케스트레이터를 새로 열었어요 ({screen})",
        "orch_spawn_tick": "오케스트레이터를 열었어요 ({screen})",
        "orch_spawn_failed": "오케스트레이터를 열지 못했어요: {error}",
        "orch_ensure_opened": "오케스트레이터를 백그라운드로 열었어요 ({screen})",
        "orch_ensure_off": "아무것도 열지 않았어요: FORAGENT_ORCH_SPAWN=off로 꺼져 있어요",
        "orch_ensure_alive": "아무것도 열지 않았어요: 오케스트레이터가 이미 살아 있어요",
        "orch_ensure_no_work": "아무것도 열지 않았어요: 승인되었거나 실행 중인 일이 없어요",
        "orch_ensure_session": "아무것도 열지 않았어요: 오케스트레이터 세션이 아직 실행 중이에요 (최근 신호 없음)",
        "orch_ensure_failed": "아무것도 열지 않았어요: Claude를 찾지 못했거나 세션을 시작하지 못했어요",
        "orch_ensure_raced": "아무것도 열지 않았어요: 방금 다른 곳에서 오케스트레이터를 열었거나 열려고 했어요. 잠시 뒤 다시 확인해요",
        "orch_ensure_help": "살아 있는 오케스트레이터가 없고 승인된 일이 있으면 백그라운드로 하나 열어요",
    },
}
