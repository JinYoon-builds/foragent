"""`fa order prepare` facts and questions shown to planning and the person (#1047)."""

MESSAGES = {
    "en": {
        "prepare_legacy": "No behaviour statements: this issue keeps its plan tests and the fa order preread route.",
        "prepare_written": "The lane wrote {count} test file(s); they wait for review and are not locked yet.",
        "prepare_question": "The lane needs an answer before its tests can be reviewed.",
        "prepare_refused_path": ("The test writer changed {path}, which it may not change: only new test files "
                                 "and helpers under {directory} can be kept. Which observable behaviour should "
                                 "the test check instead, without changing {path}?"),
        "prepare_not_text": "The test writer wrote {path} as non-UTF-8 bytes; tests must be text. How should it be written?",
        "prepare_reply_mismatch": "The writer's reply and its scratch tree disagree on {paths}.",
        "prepare_no_tests": "The writer returned neither test files nor questions.",
        "prepare_bad_reply": "The writer's reply was not the expected JSON object.",
        "prepare_failed": "The writer stopped (exit {code}{timeout}); see {log}.",
        "prepare_limit": "The writer's executor hit its usage limit; run fa order prepare again later.",
        "prepare_error": "The writer could not start: {error}",
        "prepare_stopped": "Preparation stopped while the writer was running.",
        "prepare_missing_skills": "The lane's skills {skills} are not available, so no writer started; sync the skill libraries and run fa order prepare again.",
        "prepare_context_refused": ("The lane context path {path} cannot be reproduced in the scratch copy (a link "
                                    "leading outside it or finding other content there, a link cycle, an unreadable "
                                    "entry, a socket or a device), so the writer would not see what preread sees. "
                                    "Should it become a regular file or a link inside the copied context?"),
        "prepare_busy": "Another fa order prepare for this order is running; nothing was started.",
        "prepare_none": "Not prepared yet.",
        "prepare_admission": "No machine slot became free, so the agent did not start; run fa order prepare again later: {error}",
        "prepare_main_moved": "Main moved while the tests were prepared; run fa order prepare again to prepare them on the new main.",
        "prepare_owner_stopped": "The fa order prepare that was {phase} stopped; run fa order prepare again to resume.",
        "prepare_row": "{key}: {status} — {reason}",
        "prepare_row_question": "  ? {text}",
        "prepare_help": "Let each statement issue's lane write its tests in a scratch copy of main",
        "prepare_help_status": "Only show the recorded preparation facts; start no agent",
        "prepare_plan_changed": "The plan changed while its tests were prepared; run fa order prepare again for the new requirements.",
        "prepare_review_question": "The reviewer needs planning to clarify a statement before the tests can be locked.",
        "prepare_review_held": "The reviewer held this issue's tests twice; planning must answer before another attempt.",
        "prepare_review_hold_question": ("An independent reviewer held the written tests {count} times: {reasons}. "
                                         "What should the statements say so the lane can write passing tests?"),
        "prepare_review_limit": "The reviewer's executor hit its usage limit; the review stays pending. Run fa order prepare again later.",
        "prepare_review_failed": "The reviewer stopped (exit {code}{timeout}); the review stays pending, see {log}.",
        "prepare_review_error": "The reviewer could not start, so the review stays pending: {error}",
        "prepare_review_stopped": "Preparation stopped while the reviewer was running.",
        "prepare_review_same_session": "The review ran in the writer's session; an independent session must review again.",
        "prepare_review_changed": "The written tests changed after writing ({paths}); they cannot be reviewed as written.",
        "prepare_review_bad_reply": "The reviewer's reply was not the expected JSON object; nothing was approved.",
        "prepare_review_incomplete": "The reviewer's reply lacks agreed evidence ({missing}); nothing was approved and no hold was counted.",
        "prepare_review_not_red": ("The written commands did not fail on current main ({commands}); a pass needs "
                                   "tests that fail by assertion now."),
    },
    "ko": {
        "prepare_legacy": "동작 문장이 없어요. 이 이슈는 계획의 시험과 fa order preread 경로를 그대로 써요.",
        "prepare_written": "레인이 시험 파일 {count}개를 썼어요. 검토를 기다리며 아직 잠기지 않았어요.",
        "prepare_question": "레인이 답을 받아야 시험을 검토에 넘길 수 있어요.",
        "prepare_refused_path": ("시험 작성자가 바꿀 수 없는 {path}를 바꿨어요. {directory} 아래 새 시험 파일과 "
                                 "도우미만 남길 수 있어요. {path}를 바꾸지 않고 시험이 어떤 관찰 동작을 확인해야 할까요?"),
        "prepare_not_text": "시험 작성자가 {path}를 UTF-8이 아닌 바이트로 썼어요. 시험은 글이어야 해요. 어떻게 써야 할까요?",
        "prepare_reply_mismatch": "작성자의 답과 작업 사본의 파일이 {paths}에서 달라요.",
        "prepare_no_tests": "작성자가 시험 파일도 질문도 돌려주지 않았어요.",
        "prepare_bad_reply": "작성자의 답이 약속한 JSON 객체가 아니에요.",
        "prepare_failed": "작성자가 멈췄어요(종료 {code}{timeout}). {log}를 보세요.",
        "prepare_limit": "작성자 실행기가 사용 한도에 걸렸어요. 나중에 fa order prepare를 다시 실행하세요.",
        "prepare_error": "작성자를 시작하지 못했어요: {error}",
        "prepare_stopped": "작성자가 일하는 중에 준비가 멈췄어요.",
        "prepare_missing_skills": "레인 스킬 {skills}를 찾지 못해 작성자를 시작하지 않았어요. 스킬 라이브러리를 동기화한 뒤 fa order prepare를 다시 실행하세요.",
        "prepare_context_refused": ("레인 맥락 경로 {path}를 작업 사본에 그대로 옮길 수 없어요(밖으로 가거나 다른 내용을 "
                                    "가리키는 링크, 순환 링크, 읽을 수 없는 항목, 소켓, 장치). 작성자가 preread와 같은 "
                                    "내용을 볼 수 없어요. 일반 파일이나 복사되는 맥락 안의 링크로 바꿀까요?"),
        "prepare_busy": "이 주문의 다른 fa order prepare가 실행 중이에요. 아무것도 시작하지 않았어요.",
        "prepare_none": "아직 준비하지 않았어요.",
        "prepare_admission": "기계 칸이 비지 않아 에이전트를 시작하지 않았어요. 나중에 fa order prepare를 다시 실행하세요: {error}",
        "prepare_main_moved": "시험을 준비하는 동안 main이 바뀌었어요. 새 main에서 준비하려면 fa order prepare를 다시 실행하세요.",
        "prepare_owner_stopped": "{phase} 중이던 fa order prepare가 멈췄어요. fa order prepare를 다시 실행하면 이어서 해요.",
        "prepare_row": "{key}: {status} — {reason}",
        "prepare_row_question": "  ? {text}",
        "prepare_help": "동작 문장 이슈마다 레인이 main 작업 사본에서 시험을 쓰게 해요",
        "prepare_help_status": "기록된 준비 사실만 보여 줘요. 에이전트는 시작하지 않아요",
        "prepare_plan_changed": "시험을 준비하는 동안 계획이 바뀌었어요. 새 요구에 맞게 fa order prepare를 다시 실행하세요.",
        "prepare_review_question": "검토자가 시험을 잠그기 전에 계획에서 문장을 분명히 해 달라고 해요.",
        "prepare_review_held": "검토자가 이 이슈의 시험을 두 번 보류했어요. 다시 시도하기 전에 계획이 답해야 해요.",
        "prepare_review_hold_question": ("독립 검토자가 쓴 시험을 {count}번 보류했어요: {reasons}. "
                                         "레인이 통과할 시험을 쓰려면 문장을 어떻게 고쳐야 할까요?"),
        "prepare_review_limit": "검토자 실행기가 사용 한도에 걸려 검토가 대기 중이에요. 나중에 fa order prepare를 다시 실행하세요.",
        "prepare_review_failed": "검토자가 멈췄어요(종료 {code}{timeout}). 검토는 대기 중이에요. {log}를 보세요.",
        "prepare_review_error": "검토자를 시작하지 못해 검토가 대기 중이에요: {error}",
        "prepare_review_stopped": "검토자가 일하는 중에 준비가 멈췄어요.",
        "prepare_review_same_session": "검토가 작성자 세션에서 돌았어요. 독립된 세션이 다시 검토해야 해요.",
        "prepare_review_changed": "쓴 시험이 작성 뒤에 바뀌었어요({paths}). 쓴 그대로 검토할 수 없어요.",
        "prepare_review_bad_reply": "검토자의 답이 약속한 JSON 객체가 아니에요. 아무것도 승인하지 않았어요.",
        "prepare_review_incomplete": "검토자의 답에 약속한 증거가 없어요({missing}). 아무것도 승인하지 않았고 보류로 세지도 않았어요.",
        "prepare_review_not_red": ("쓴 명령이 현재 main에서 실패하지 않았어요({commands}). 통과하려면 지금 단언으로 "
                                   "실패하는 시험이 있어야 해요."),
    },
}
