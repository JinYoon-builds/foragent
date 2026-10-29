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
        "scorecard_role_signals": ("signals {raised} raised · {called} called · {not_now} not now · "
                                   "{unanswered} unanswered"),
        "scorecard_role_signals_pair": "{today} (before {before})",
        "scorecard_repeated": "Failed again for the same reason: {items}",
        "scorecard_unexplained": "Failed without a reason: {issues}",
        "scorecard_worst": "Worse than the days before: {metric} {before} -> {now}",
        "scorecard_label_runs": "runs",
        "scorecard_label_merge_rate": "merge rate",
        "scorecard_label_median_minutes_to_merge": "minutes to merge",
        "scorecard_label_github_calls_per_hour": "GitHub calls per hour",
        "scorecard_label_review_returns": "review returns",
        "scorecard_label_reverts_after_merge": "reverts after merge",
        "scorecard_label_spec_questions": "spec questions",
        "scorecard_label_load": "load",
        "scorecard_label_phase": "{phase} minutes",
        "scorecard_label_phase_implementing": "implementing minutes",
        "scorecard_label_phase_checking": "checking minutes",
        "scorecard_label_phase_test-slot-wait": "minutes waiting for a test slot",
        "scorecard_label_phase_baseline": "baseline minutes",
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
        "scorecard_role_signals": "신호 {raised}건 · 부름 {called}건 · 나중에 {not_now}건 · 답 없음 {unanswered}건",
        "scorecard_role_signals_pair": "{today} (그 전 {before})",
        "scorecard_repeated": "같은 이유로 여러 번 실패했어요: {items}",
        "scorecard_unexplained": "이유 없이 실패했어요: {issues}",
        "scorecard_worst": "그 전 며칠보다 나빠졌어요: {metric} {before} -> {now}",
        "scorecard_label_runs": "실행 수",
        "scorecard_label_merge_rate": "머지 비율",
        "scorecard_label_median_minutes_to_merge": "머지까지 걸린 분",
        "scorecard_label_github_calls_per_hour": "시간당 GitHub 호출",
        "scorecard_label_review_returns": "리뷰 반려",
        "scorecard_label_reverts_after_merge": "머지 뒤 되돌림",
        "scorecard_label_spec_questions": "명세 질문",
        "scorecard_label_load": "부하",
        "scorecard_label_phase": "{phase} 분",
        "scorecard_label_phase_implementing": "만드는 데 걸린 분",
        "scorecard_label_phase_checking": "확인하는 데 걸린 분",
        "scorecard_label_phase_test-slot-wait": "시험 자리를 기다린 분",
        "scorecard_label_phase_baseline": "기준선에 걸린 분",
    },
}
