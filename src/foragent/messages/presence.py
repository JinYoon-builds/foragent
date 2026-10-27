"""The Foragent presence segment on the Claude Code statusline (#754)."""

MESSAGES = {
    "en": {
        "scorecard_flow_today": ("  flow (last 24 h): {idle} idle min, {waste} runtime wasted, wait min ({waits}), "
                                 "{lead} min approval to merge, {people} person interventions, {merges} merges"),
        "scorecard_flow_previous": ("  flow (previous days, daily median): {idle} idle min, {waste} runtime wasted, "
                                    "wait min ({waits}), {lead} min approval to merge, "
                                    "{people} person interventions, {merges} merges"),
        "scorecard_binding": "Binding constraint now: {now}",
        "scorecard_binding_changed": "Binding constraint now: {now} (changed from {before} the previous day)",
        "presence_statusline": "🐿 {lanes} lanes working · ✋ {turns} · {merged} merged today",
    },
    "ko": {
        "scorecard_flow_today": ("  흐름(최근 24시간): 놀린 {idle}분, 버린 실행 시간 {waste}, 기다린 분 ({waits}), "
                                 "승인부터 머지까지 {lead}분, 사람 개입 {people}건, 머지 {merges}건"),
        "scorecard_flow_previous": ("  흐름(그 전 며칠의 하루 중앙값): 놀린 {idle}분, 버린 실행 시간 {waste}, "
                                    "기다린 분 ({waits}), 승인부터 머지까지 {lead}분, 사람 개입 {people}건, 머지 {merges}건"),
        "scorecard_binding": "지금 묶는 제약: {now}",
        "scorecard_binding_changed": "지금 묶는 제약: {now} (전날 {before}에서 바뀜)",
        "presence_statusline": "🐿 레인 {lanes} 일하는 중 · ✋ {turns} · 오늘 머지 {merged}",
    },
}
