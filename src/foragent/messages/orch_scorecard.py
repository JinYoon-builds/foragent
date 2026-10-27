"""Lines for `fa orch scorecard`."""

MESSAGES = {
    "en": {
        "scorecard_today": ("Last 24 h: {runs} runs, {merge_rate} merged, {merge_minutes} min to merge, "
                            "{calls} GitHub calls/h, {returns} review returns, {reverts} reverts, "
                            "{questions} spec questions, load {load}, phase min: {phases}"),
        "scorecard_previous": ("Daily median of the {days} days before: {runs} runs, {merge_rate} merged, "
                               "{merge_minutes} min to merge, {calls} GitHub calls/h, {returns} review returns, "
                               "{reverts} reverts, {questions} spec questions, load {load}, phase min: {phases}"),
        "scorecard_worse": "Got worse: {metric} {before} -> {now}",
        "scorecard_nothing_worse": "Nothing got worse than the days before.",
        "scorecard_repeated": "Failed again for the same reason: {items}",
        "scorecard_unexplained": "Failed without a reason: {issues}",
    },
    "ko": {
        "scorecard_today": ("최근 24시간: 실행 {runs}건, 머지 {merge_rate}, 머지까지 {merge_minutes}분, "
                            "GitHub 호출 시간당 {calls}회, 리뷰 반려 {returns}건, 되돌림 {reverts}건, "
                            "명세 질문 {questions}건, 부하 {load}, 단계별 분: {phases}"),
        "scorecard_previous": ("그 전 {days}일의 하루 중앙값: 실행 {runs}건, 머지 {merge_rate}, "
                               "머지까지 {merge_minutes}분, GitHub 호출 시간당 {calls}회, 리뷰 반려 {returns}건, "
                               "되돌림 {reverts}건, 명세 질문 {questions}건, 부하 {load}, 단계별 분: {phases}"),
        "scorecard_worse": "나빠졌어요: {metric} {before} -> {now}",
        "scorecard_nothing_worse": "그 전 며칠보다 나빠진 것이 없어요",
        "scorecard_repeated": "같은 이유로 여러 번 실패했어요: {items}",
        "scorecard_unexplained": "이유 없이 실패했어요: {issues}",
    },
}
