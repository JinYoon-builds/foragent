"""Foragent presence in Claude Code: the statusline segment (#754) and the session-start line (#755)."""

MESSAGES = {
    "en": {
        "scorecard_flow_today": ("  flow (last 24 h): legacy combined idle {idle} min (run or test slot unused), "
                                 "run slot idle {run_idle} min, test slot idle {test_idle} min, "
                                 "{waste} of runtime ended unmerged (unmerged runtime / all runtime), "
                                 "wait min inside runs only ({waits}), {lead} min latest approval to merge (median), "
                                 "{people} person interventions, {merges} merges"),
        "scorecard_flow_previous": ("  flow (previous days, daily median): legacy combined idle {idle} min "
                                    "(run or test slot unused), run slot idle {run_idle} min, "
                                    "test slot idle {test_idle} min, {waste} of runtime ended unmerged "
                                    "(unmerged runtime / all runtime), wait min inside runs only ({waits}), "
                                    "{lead} min latest approval to merge (median), "
                                    "{people} person interventions, {merges} merges"),
        "scorecard_binding": "Binding constraint now: {now}",
        "scorecard_binding_changed": "Binding constraint now: {now} (changed from {before} the previous day)",
        "presence_statusline": "🐿 {lanes} lanes working · ✋ {turns} · {merged} merged today",
        "presence_open_question": "1 question needs your answer in the planning session",
        "presence_open_questions": "{count} questions need your answer in the planning session",
        "presence_session_start": "since the last report: {merged} merges · ✋ {turns} · /fa for the report",
        "presence_title_pm": "PM · {project}",
        "presence_title_role": "{name} · {project}",
        "presence_title_lane": "lane {name} · {project}",
    },
    "ko": {
        "scorecard_flow_today": ("  흐름(최근 24시간): 예전 합산 놀림 {idle}분(작업·시험 자리 중 하나라도 빔), "
                                 "작업 자리 놀림 {run_idle}분, 시험 자리 놀림 {test_idle}분, "
                                 "머지 없이 끝난 실행 시간 {waste}(머지 없이 끝난 실행 시간 / 전체 실행 시간), "
                                 "실행 안에서만 기다린 분 ({waits}), 최근 승인부터 머지까지 {lead}분(중앙값), "
                                 "사람 개입 {people}건, 머지 {merges}건"),
        "scorecard_flow_previous": ("  흐름(그 전 며칠의 하루 중앙값): 예전 합산 놀림 {idle}분(작업·시험 자리 중 하나라도 빔), "
                                    "작업 자리 놀림 {run_idle}분, 시험 자리 놀림 {test_idle}분, "
                                    "머지 없이 끝난 실행 시간 {waste}(머지 없이 끝난 실행 시간 / 전체 실행 시간), "
                                    "실행 안에서만 기다린 분 ({waits}), 최근 승인부터 머지까지 {lead}분(중앙값), "
                                    "사람 개입 {people}건, 머지 {merges}건"),
        "scorecard_binding": "지금 묶는 제약: {now}",
        "scorecard_binding_changed": "지금 묶는 제약: {now} (전날 {before}에서 바뀜)",
        "presence_statusline": "🐿 레인 {lanes} 일하는 중 · ✋ {turns} · 오늘 머지 {merged}",
        "presence_open_question": "기획 세션에 물어볼 게 1개 있어요",
        "presence_open_questions": "기획 세션에 물어볼 게 {count}개 있어요",
        "presence_session_start": "지난 보고 이후 머지 {merged} · ✋ {turns} · /fa 로 보고 보기",
        "presence_title_pm": "PM · {project}",
        "presence_title_role": "{name} · {project}",
        "presence_title_lane": "lane {name} · {project}",
    },
}
