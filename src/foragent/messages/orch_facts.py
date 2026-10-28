"""Review-round and resource lines for `fa orch facts`."""

MESSAGES = {
    "en": {
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
    },
    "ko": {
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
    },
}
