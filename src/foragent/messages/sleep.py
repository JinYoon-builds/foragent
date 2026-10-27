"""The gate tick's line after the machine wakes from sleep (#415)."""

MESSAGES = {
    "en": {
        "sleep_wake": (
            "Machine slept {hours} h {minutes} min; stall checks wait until {until} "
            "or until a run shows activity."
        ),
    },
    "ko": {
        "sleep_wake": "기계가 {hours}시간 {minutes}분 잠들었어요. {until}까지 또는 실행에 새 활동이 보일 때까지 멈춤 검사를 미뤄요",
    },
}
