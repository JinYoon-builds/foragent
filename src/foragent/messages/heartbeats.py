"""Lines for `fa orch alive`: its help and its output without --json."""

MESSAGES = {
    "en": {
        "heartbeats_help": "Show whether an orchestrator renewed its heartbeat lately",
        "heartbeats_never": "No orchestrator has run here yet.",
        "heartbeats_alive": "An orchestrator is running (last seen {age} s ago, {at}).",
        "heartbeats_gone": "No orchestrator is running (last seen {age} s ago, {at}).",
    },
    "ko": {
        "heartbeats_help": "오케스트레이터가 최근에 생존 신호를 갱신했는지 보여 줘요",
        "heartbeats_never": "여기서 오케스트레이터가 아직 실행되지 않았어요",
        "heartbeats_alive": "오케스트레이터가 실행 중이에요 ({age}초 전 확인, {at})",
        "heartbeats_gone": "오케스트레이터가 실행 중이 아니에요 (마지막 확인 {age}초 전, {at})",
    },
}
