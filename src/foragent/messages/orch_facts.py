"""Review-round and baseline-capture lines for `fa orch facts`."""

MESSAGES = {
    "en": {
        "facts_review_rounds": ("Review rounds per merged run (last {days} days): median {median}; "
                                "most: #{issue} with {rounds} review rounds"),
        "facts_review_rounds_none": "Review rounds per merged run (last {days} days): none",
        "facts_repeated_class": ("Most repeated finding class: {slug}, {within} repeats within runs, "
                                 "{across} across runs ({issues})"),
        "facts_baseline": "Baseline capture: latest {duration} s, {share}% of the {limit} s limit",
        "facts_baseline_near": "; near the limit",
        "facts_baseline_none": "Baseline capture: no measured capture yet (limit {limit} s)",
    },
    "ko": {
        "facts_review_rounds": ("머지된 실행의 리뷰 라운드 (최근 {days}일): 중앙값 {median}; "
                                "가장 많음: #{issue}, 리뷰 라운드 {rounds}회"),
        "facts_review_rounds_none": "머지된 실행의 리뷰 라운드 (최근 {days}일): 없음",
        "facts_repeated_class": ("가장 많이 반복된 지적 종류: {slug}, 실행 안에서 {within}회, "
                                 "실행 사이에서 {across}회 반복 ({issues})"),
        "facts_baseline": "기준선 캡처: 최근 {duration} s, 한도 {limit} s의 {share}%",
        "facts_baseline_near": "; 한도에 가까움",
        "facts_baseline_none": "기준선 캡처: 아직 측정된 캡처 없음 (한도 {limit} s)",
    },
}
