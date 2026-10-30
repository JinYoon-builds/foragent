"""Lines for the orchestrator seat lease (#962)."""

MESSAGES = {
    "en": {
        "orch_seat_refused": "Another orchestrator already holds the seat; this session should end.",
        "orch_seat_help": "Show which orchestrator session holds this project's seat",
        "orch_seat_json_help": "Print the seat as JSON",
        "orch_seat_free": "The orchestrator seat is free.",
        "orch_seat_live": "The orchestrator seat is held by PID {pid} since {taken_at}.",
        "orch_seat_gone": "The orchestrator seat names PID {pid}, which is no longer running.",
        "orch_seat_unknown": "The orchestrator seat names PID {pid}; whether it runs cannot be read.",
        "orch_ensure_seat": "Opened nothing: the orchestrator holding the seat is still running.",
    },
    "ko": {
        "orch_seat_refused": "다른 오케스트레이터가 이미 자리를 잡고 있어요. 이 세션은 끝내요",
        "orch_seat_help": "이 프로젝트의 오케스트레이터 자리를 누가 잡고 있는지 보여 줘요",
        "orch_seat_json_help": "자리 정보를 JSON으로 출력해요",
        "orch_seat_free": "오케스트레이터 자리가 비어 있어요",
        "orch_seat_live": "PID {pid}가 {taken_at}부터 오케스트레이터 자리를 잡고 있어요",
        "orch_seat_gone": "오케스트레이터 자리에 적힌 PID {pid}는 더 이상 실행 중이 아니에요",
        "orch_seat_unknown": "오케스트레이터 자리에 적힌 PID {pid}가 실행 중인지 읽을 수 없어요",
        "orch_ensure_seat": "아무것도 열지 않았어요: 자리를 잡은 오케스트레이터가 아직 실행 중이에요",
    },
}
