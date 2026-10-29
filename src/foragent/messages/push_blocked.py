"""Lines for the gate's push when work becomes blocked or the orchestrator is off (#129)."""

MESSAGES = {
    "en": {
        "push_blocked_title": "Foragent",
        "push_blocked_issue": "#{number} is waiting for a person ({label}{reason})",
        "push_blocked_orch_off": "The orchestrator is off while work runs in {repo}.",
        "push_blocked_tick": "Pushed: {items}",
        "push_blocked_tick_orch_off": "orchestrator off",
        "push_blocked_failed": "Could not push blocked work: {error}",
    },
    "ko": {
        "push_blocked_title": "Foragent",
        "push_blocked_issue": "#{number}이(가) 사람을 기다려요 ({label}{reason})",
        "push_blocked_orch_off": "{repo}에서 일이 도는데 오케스트레이터가 꺼져 있어요.",
        "push_blocked_tick": "알림을 보냈어요: {items}",
        "push_blocked_tick_orch_off": "오케스트레이터 꺼짐",
        "push_blocked_failed": "막힌 일 알림을 보내지 못했어요: {error}",
    },
}
