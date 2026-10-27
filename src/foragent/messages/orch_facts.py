"""Review-round lines for `fa orch facts`."""

MESSAGES = {
    "en": {
        "facts_review_rounds": ("Review rounds per merged run (last {days} days): median {median}; "
                                "most: #{issue} with {rounds} review rounds"),
        "facts_review_rounds_none": "Review rounds per merged run (last {days} days): none",
        "facts_repeated_class": ("Most repeated finding class: {slug}, {within} repeats within runs, "
                                 "{across} across runs ({issues})"),
    },
    "ko": {
        "facts_review_rounds": ("머지된 실행의 리뷰 라운드 (최근 {days}일): 중앙값 {median}; "
                                "가장 많음: #{issue}, 리뷰 라운드 {rounds}회"),
        "facts_review_rounds_none": "머지된 실행의 리뷰 라운드 (최근 {days}일): 없음",
        "facts_repeated_class": ("가장 많이 반복된 지적 종류: {slug}, 실행 안에서 {within}회, "
                                 "실행 사이에서 {across}회 반복 ({issues})"),
    },
}
