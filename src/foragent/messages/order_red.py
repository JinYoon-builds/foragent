"""`fa order red`: whether a lane's test draft fails on the fetched base for a real reason (#1725)."""

MESSAGES = {
    "en": {
        "order_red_help": "Check that a test draft fails on the fetched base branch for a real reason",
        "order_red_yes": "Red on base {base}: every test in {files} fails by a real failure.",
        "order_red_no": "Not red on base {base}: {count} item(s) need fixing.",
        "order_red_passes": "{test} already passes",
        "order_red_skipped": "{test} is skipped",
        "order_red_xfail": "{test} is marked xfail",
        "order_red_not_run": "{test} did not run",
        "order_red_missing": "{test} fails only because module {module} is missing",
        "order_red_no_exception": "{test} fails without an exception",
        "order_red_empty": "{file} collects no test",
        "order_red_collect_error": "{file} does not collect: {error}",
        "order_red_setup_error": "{test} errors in setup: {error}",
        "order_red_teardown_error": "{test} errors in teardown: {error}",
        "order_red_no_files": "the plan names no acceptance file to check",
        "order_red_slot_timeout": "no machine test slot became free before the deadline",
        "order_red_timeout": "the draft did not finish within {seconds} seconds",
        "order_red_unavailable": (
            "Could not check the draft, so there is no verdict. Files: {files}. Command: {command}. Cause: {cause}"
        ),
    },
    "ko": {
        "order_red_help": "시험 초안이 가져온 기준 브랜치에서 진짜 이유로 실패하는지 확인해요",
        "order_red_yes": "기준 {base}에서 빨간불이에요: {files}의 모든 시험이 진짜 실패로 떨어져요.",
        "order_red_no": "기준 {base}에서 빨간불이 아니에요: 고칠 항목이 {count}개 있어요.",
        "order_red_passes": "{test}는 이미 통과해요",
        "order_red_skipped": "{test}는 건너뛰어요",
        "order_red_xfail": "{test}는 xfail로 표시돼 있어요",
        "order_red_not_run": "{test}는 돌지 않았어요",
        "order_red_missing": "{test}는 모듈 {module}이 없어서만 실패해요",
        "order_red_no_exception": "{test}는 예외 없이 실패해요",
        "order_red_empty": "{file}에서 수집되는 시험이 없어요",
        "order_red_collect_error": "{file}가 수집되지 않아요: {error}",
        "order_red_setup_error": "{test}가 준비 단계에서 오류가 나요: {error}",
        "order_red_teardown_error": "{test}가 정리 단계에서 오류가 나요: {error}",
        "order_red_no_files": "계획에 확인할 인수 파일이 없어요",
        "order_red_slot_timeout": "마감 전에 기계 시험 칸이 비지 않았어요",
        "order_red_timeout": "초안이 {seconds}초 안에 끝나지 않았어요",
        "order_red_unavailable": (
            "초안을 확인할 수 없어서 판정이 없어요. 파일: {files}. 명령: {command}. 원인: {cause}"
        ),
    },
}
