"""Machine workload admission messages (capacity.hold_workload, #991)."""

MESSAGES = {
    "en": {
        "workload_wait": (
            "Waiting for machine capacity ({reason}): {used}/{budget} budget slots used, {free} free; "
            "held by {holders}. Waiting up to {seconds}s; press Ctrl-C to cancel and retry later."
        ),
        "workload_timeout": (
            "Gave up waiting for machine capacity after {seconds}s ({reason}): {used}/{budget} budget slots used, "
            "{free} free; held by {holders}. Retry the same command when one of them finishes or the load drops."
        ),
        "workload_test_wait": (
            "Waiting for a machine test slot ({reason}): {used}/{budget} test slots used, {free} free; "
            "Foragent work running: {holders}. Waiting up to {seconds}s in total; press Ctrl-C to cancel and retry later."
        ),
        "workload_test_timeout": (
            "Gave up waiting for a machine test slot after {seconds}s in total ({reason}): {used}/{budget} test slots "
            "used, {free} free; Foragent work running: {holders}. Retry the same command when a test run finishes."
        ),
        "workload_reason_tests": "every test slot is in use",
        "workload_reason_busy": "another Foragent process kept the machine lock",
        "workload_reason_full": "budget full",
        "workload_reason_overload": "machine load too high",
        "workload_reason_critical": "critical memory pressure",
        "workload_holders_none": "no Foragent work",
    },
    "ko": {
        "workload_wait": (
            "기계 여유를 기다려요({reason}): 예산 {budget}칸 중 {used}칸 사용, {free}칸 남음. "
            "사용 중: {holders}. 최대 {seconds}초 기다려요. 취소하려면 Ctrl-C를 누르고 나중에 다시 실행하세요."
        ),
        "workload_timeout": (
            "{seconds}초 동안 기계 여유가 나지 않아 멈췄어요({reason}): 예산 {budget}칸 중 {used}칸 사용, {free}칸 남음. "
            "사용 중: {holders}. 이 작업 중 하나가 끝나거나 부하가 내려가면 같은 명령을 다시 실행하세요."
        ),
        "workload_test_wait": (
            "기계 시험 칸을 기다려요({reason}): 시험 칸 {budget}개 중 {used}개 사용, {free}개 남음. "
            "실행 중인 Foragent 작업: {holders}. 모두 합쳐 최대 {seconds}초 기다려요. 취소하려면 Ctrl-C를 누르고 나중에 다시 실행하세요."
        ),
        "workload_test_timeout": (
            "모두 합쳐 {seconds}초 동안 시험 칸이 나지 않아 멈췄어요({reason}): 시험 칸 {budget}개 중 {used}개 사용, {free}개 남음. "
            "실행 중인 Foragent 작업: {holders}. 시험 하나가 끝나면 같은 명령을 다시 실행하세요."
        ),
        "workload_reason_tests": "시험 칸이 모두 사용 중이에요",
        "workload_reason_busy": "다른 Foragent 프로세스가 기계 잠금을 쥐고 있어요",
        "workload_reason_full": "예산이 다 찼어요",
        "workload_reason_overload": "기계 부하가 너무 높아요",
        "workload_reason_critical": "메모리 압박이 심각해요",
        "workload_holders_none": "Foragent 작업 없음",
    },
}
