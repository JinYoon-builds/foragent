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
        "prepare_context_refused": ("The lane context path {path} is a link leading outside the repository, a link "
                                    "cycle or a special file, so the writer would not see what preread sees. "
                                    "Should it become a regular file or a link inside the repository?"),
        "prepare_busy": "Another fa order prepare for this order is running; nothing was started.",
        "prepare_none": "Not prepared yet.",
        "prepare_row": "{key}: {status} — {reason}",
        "prepare_row_question": "  ? {text}",
        "prepare_help": "Let each statement issue's lane write its tests in a scratch copy of main",
        "prepare_help_status": "Only show the recorded preparation facts; start no agent",
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
        "prepare_context_refused": ("레인 맥락 경로 {path}가 저장소 밖으로 가는 링크, 순환 링크, 또는 특수 파일이라 "
                                    "작성자가 preread와 같은 내용을 볼 수 없어요. 일반 파일이나 저장소 안 링크로 바꿀까요?"),
        "prepare_busy": "이 주문의 다른 fa order prepare가 실행 중이에요. 아무것도 시작하지 않았어요.",
        "prepare_none": "아직 준비하지 않았어요.",
        "prepare_row": "{key}: {status} — {reason}",
        "prepare_row_question": "  ? {text}",
        "prepare_help": "동작 문장 이슈마다 레인이 main 작업 사본에서 시험을 쓰게 해요",
        "prepare_help_status": "기록된 준비 사실만 보여 줘요. 에이전트는 시작하지 않아요",
    },
}
