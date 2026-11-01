"""Review-round, resource and baseline-capture lines for `fa orch facts`."""

MESSAGES = {
    "en": {
        "facts_schedule_role": ("Scheduled {role}: {state}; orders {orders}; input cutoff {cutoff}; last attempt {attempt}; "
                                "last error {error}; next retry {retry}; pending age {age}"),
        "facts_schedule_unknown": "none",
        "facts_schedule_unavailable": "unavailable (role not configured)",
        "facts_schedule_held": "due, held ({reason})",
        "facts_schedule_order_finished": "due (order finished)",
        "facts_schedule_no_new_input": "not due (no new merge or run records)",
        "facts_schedule_no_finished_order": "not due (no newly finished order)",
        "facts_schedule_pending_request": "waiting for its pending request",
        "facts_schedule_running": "session running",
        "facts_review_rounds": ("Review rounds per merged run (last {days} days): median {median}; "
                                "most: #{issue} with {rounds} review rounds"),
        "facts_review_rounds_none": "Review rounds per merged run (last {days} days): none",
        "facts_repeated_class": ("Most repeated finding class: {slug}, {within} repeats within runs, "
                                 "{across} across runs ({issues})"),
        "facts_resources": ("Resources (sample age {age}): CPU busy {cpu}, load {load} per CPU, memory {memory}, "
                            "disk {disk}, GitHub API remaining {github}, test-slot queue {queue}, "
                            "executors {executors}"),
        "facts_resources_disk": "read {read:.1f} KiB/s {read_ops:.1f} ops/s, write {write:.1f} KiB/s {write_ops:.1f} ops/s",
        "facts_resources_unknown": "unknown",
        "facts_resources_ok": "ok",
        "facts_resources_limited": "limited until {until}",
        "facts_baseline": "Baseline capture: latest {duration} s, {share}% of the {limit} s limit",
        "facts_baseline_near": "; near the limit",
        "facts_baseline_none": "Baseline capture: no measured capture yet (limit {limit} s)",
    },
    "ko": {
        "facts_schedule_role": ("예약 {role}: {state}; 주문 {orders}; 입력 기준 {cutoff}; 마지막 시도 {attempt}; "
                                "마지막 오류 {error}; 다음 재시도 {retry}; 대기 시간 {age}"),
        "facts_schedule_unknown": "없음",
        "facts_schedule_unavailable": "쓸 수 없음 (역할이 설정되지 않음)",
        "facts_schedule_held": "차례, 보류됨 ({reason})",
        "facts_schedule_order_finished": "차례 (주문 끝남)",
        "facts_schedule_no_new_input": "차례 아님 (새 머지나 실행 기록 없음)",
        "facts_schedule_no_finished_order": "차례 아님 (새로 끝난 주문 없음)",
        "facts_schedule_pending_request": "대기 중인 요청을 기다리는 중",
        "facts_schedule_running": "세션 실행 중",
        "facts_review_rounds": ("머지된 실행의 리뷰 라운드 (최근 {days}일): 중앙값 {median}; "
                                "가장 많음: #{issue}, 리뷰 라운드 {rounds}회"),
        "facts_review_rounds_none": "머지된 실행의 리뷰 라운드 (최근 {days}일): 없음",
        "facts_repeated_class": ("가장 많이 반복된 지적 종류: {slug}, 실행 안에서 {within}회, "
                                 "실행 사이에서 {across}회 반복 ({issues})"),
        "facts_resources": ("자원 (시료 {age} 전): CPU 사용 {cpu}, CPU당 부하 {load}, 메모리 {memory}, "
                            "디스크 {disk}, GitHub API 남은 호출 {github}, 시험 자리 대기 {queue}, "
                            "실행기 {executors}"),
        "facts_resources_disk": "읽기 {read:.1f} KiB/s {read_ops:.1f}회/s, 쓰기 {write:.1f} KiB/s {write_ops:.1f}회/s",
        "facts_resources_unknown": "모름",
        "facts_resources_ok": "정상",
        "facts_resources_limited": "{until}까지 한도",
        "facts_baseline": "기준선 캡처: 최근 {duration} s, 한도 {limit} s의 {share}%",
        "facts_baseline_near": "; 한도에 가까움",
        "facts_baseline_none": "기준선 캡처: 아직 측정된 캡처 없음 (한도 {limit} s)",
    },
}
