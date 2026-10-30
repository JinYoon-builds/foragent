"""Lines for a live orchestrator that stays silent past the alert threshold (#952)."""

MESSAGES = {
    "en": {
        "orch_silent_title": "Foragent: orchestrator silent",
        "orch_silent_alert": ("The orchestrator is running but silent. It may be waiting on a question "
                              "no one can see. Run claude agents and look for orch: {directory}."),
        "settings_window_label_orch_silent_alert_minutes": "silent orchestrator alert minutes",
        "settings_desc_orch_silent_alert_minutes": (
            "Minutes a running orchestrator may stay silent before one push reaches you, 0 to 1440; "
            "0 turns the push off."),
    },
    "ko": {
        "orch_silent_title": "Foragent: 오케스트레이터 조용함",
        "orch_silent_alert": ("오케스트레이터가 조용해요. 아직 실행 중이고, 보이지 않는 질문에 답을 기다리고 "
                              "있을 수 있어요. claude agents에서 orch: {directory} 세션을 확인해요."),
        "settings_window_label_orch_silent_alert_minutes": "조용한 오케스트레이터 알림 분",
        "settings_desc_orch_silent_alert_minutes": (
            "실행 중인 오케스트레이터가 이 분 수만큼 조용하면 알림을 한 번 보내요. 0에서 1440까지이고, "
            "0이면 알림을 보내지 않아요."),
    },
}
