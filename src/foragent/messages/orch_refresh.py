"""Lines for `fa orch refresh-due`, `fa orch handoff` and `fa orch refresh` (#413)."""

MESSAGES = {
    "en": {
        "orch_refresh_due_help": "Report the orchestrator session's compactions and age against the refresh thresholds",
        "orch_handoff_help": "Check and store the outgoing orchestrator's handoff note",
        "orch_refresh_help": "Open a fresh orchestrator session from the stored handoff note",
        "orch_refresh_ready_help": "Confirm the handoff note is written and nobody is talking with you",
        "orch_refresh_no_session": "Not due: the heartbeat names no orchestrator session, so nothing can be measured.",
        "orch_refresh_no_transcript": "Not due: the session transcript is missing ({path}).",
        "orch_refresh_due_compactions": "Due: {count} compactions reached the limit of {limit}.",
        "orch_refresh_due_age": "Due: the session is {hours} hours old, at or past the limit of {limit} hours.",
        "orch_refresh_due_both": ("Due: {count} compactions reached the limit of {limit}, "
                                  "and the session is {hours} hours old (limit {limit_hours})."),
        "orch_refresh_not_due": ("Not due: {count} of {limit} compactions, "
                                 "{hours} of {limit_hours} hours."),
        "orch_handoff_missing_file": "Cannot read the handoff note {path}: {error}",
        "orch_handoff_invalid": ("The handoff note is not complete. Write these sections, or `none` in each: "
                                 "{sections}."),
        "orch_handoff_saved": "Handoff note saved: {path}",
        "orch_refresh_needs_ready": ("Refresh needs --ready. Write the handoff note, wait until nobody is talking "
                                     "with you, then run `fa orch refresh --ready`."),
        "orch_refresh_no_handoff": ("No fresh handoff note. Write one after the last refresh with "
                                    "`fa orch handoff --file NOTE.md`."),
        "orch_refresh_bad_handoff": "The stored handoff note is no longer complete: {sections}. Store it again with `fa orch handoff --file`.",
        "orch_refresh_person": ("Not now: a person wrote to the inbox in the last {minutes} minutes and it is "
                                "not done ({ids}). Finish it and mark it done with `fa orch request-status`."),
        "orch_refresh_questions": ("Not now: these questions are open or were answered in the last {minutes} "
                                   "minutes: {ids}. Check them with `fa orch questions --json` and the inbox."),
        "orch_refresh_failed": ("Could not open a fresh orchestrator session (spawning is off, Claude is missing, "
                                "or the launch failed). This session stays active and the handoff note is kept."),
        "orch_refresh_done": ("Opened fresh orchestrator session {new}. This session ({old}) is retired: "
                              "end it yourself now."),
    },
    "ko": {
        "orch_refresh_due_help": "오케스트레이터 세션의 압축 횟수와 나이를 교체 기준과 비교해 알려요",
        "orch_handoff_help": "물러나는 오케스트레이터의 인계 노트를 확인하고 저장해요",
        "orch_refresh_help": "저장한 인계 노트로 새 오케스트레이터 세션을 열어요",
        "orch_refresh_ready_help": "인계 노트를 썼고 대화 중인 사람이 없음을 확인해요",
        "orch_refresh_no_session": "아직 아니에요: 하트비트에 오케스트레이터 세션이 없어서 잴 수 없어요",
        "orch_refresh_no_transcript": "아직 아니에요: 세션 기록 파일이 없어요 ({path})",
        "orch_refresh_due_compactions": "교체할 때예요: 압축이 {count}번으로 기준 {limit}번에 닿았어요",
        "orch_refresh_due_age": "교체할 때예요: 세션이 {hours}시간 되어 기준 {limit}시간에 닿았어요",
        "orch_refresh_due_both": "교체할 때예요: 압축 {count}번(기준 {limit}번), 세션 {hours}시간(기준 {limit_hours}시간)",
        "orch_refresh_not_due": "아직 아니에요: 압축 {count}/{limit}번, {hours}/{limit_hours}시간",
        "orch_handoff_missing_file": "인계 노트 {path}를 읽지 못했어요: {error}",
        "orch_handoff_invalid": "인계 노트가 완전하지 않아요. 이 절을 쓰거나 각각 `none`이라고 적어 주세요: {sections}",
        "orch_handoff_saved": "인계 노트를 저장했어요: {path}",
        "orch_refresh_needs_ready": ("교체하려면 --ready가 필요해요. 인계 노트를 쓰고, 대화 중인 사람이 없을 때 "
                                     "`fa orch refresh --ready`를 실행해요"),
        "orch_refresh_no_handoff": "새 인계 노트가 없어요. 마지막 교체 뒤에 `fa orch handoff --file NOTE.md`로 저장해요",
        "orch_refresh_bad_handoff": "저장한 인계 노트가 더는 완전하지 않아요: {sections}. `fa orch handoff --file`로 다시 저장해요",
        "orch_refresh_person": ("지금은 안 돼요: 사람이 최근 {minutes}분 안에 inbox에 남긴 요청이 끝나지 않았어요 "
                                "({ids}). 마무리하고 `fa orch request-status`로 done 표시해요"),
        "orch_refresh_questions": ("지금은 안 돼요: 열려 있거나 최근 {minutes}분 안에 답한 질문이 있어요: {ids}. "
                                   "`fa orch questions --json`과 inbox를 확인해요"),
        "orch_refresh_failed": ("새 오케스트레이터 세션을 열지 못했어요(spawn 꺼짐, Claude 없음, 또는 실행 실패). "
                                "이 세션이 계속 맡고 인계 노트는 그대로 남아요"),
        "orch_refresh_done": "새 오케스트레이터 세션 {new}을 열었어요. 이 세션({old})은 물러났으니 스스로 끝내요",
    },
}
