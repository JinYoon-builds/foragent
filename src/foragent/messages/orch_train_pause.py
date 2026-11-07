"""`fa orch pause-trains` and `fa orch resume-trains`: hold and resume the merge owner's trains."""

MESSAGES = {
    "en": {
        "orch_trains_paused": "Train building paused; the running train finishes and publications keep "
                              "landing. Why: {why}{until}. fa orch resume-trains ends it.",
        "orch_trains_paused_until": " (until {until})",
        "orch_trains_resumed": "Train building resumed; the merge owner builds trains again in its next pass.",
        "orch_trains_not_paused": "Train building was not paused; nothing changed.",
        "orch_trains_why_required": "fa orch pause-trains: --why must say why trains pause; nothing was changed.",
        "status_trains_paused": "Train building paused: {why}{until}",
    },
    "ko": {
        "orch_trains_paused": "열차 만들기를 멈췄어요. 달리는 열차는 끝까지 가고 발행은 계속 들어가요. "
                              "이유: {why}{until}. 다시 시작: fa orch resume-trains",
        "orch_trains_paused_until": " ({until}까지)",
        "orch_trains_resumed": "열차 만들기를 다시 시작했어요. 다음 차례부터 열차를 다시 만들어요.",
        "orch_trains_not_paused": "열차 만들기가 멈춰 있지 않았어요. 바뀐 것은 없어요.",
        "orch_trains_why_required": "fa orch pause-trains: --why에 멈추는 이유를 적어 주세요. 바뀐 것은 없어요.",
        "status_trains_paused": "열차 만들기 멈춤 (train pause): {why}{until}",
    },
}
