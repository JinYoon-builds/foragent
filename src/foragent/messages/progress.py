"""Strings for the combined progress section of the lane view."""

MESSAGES = {
    "en": {
        "progress_bar": "progress {bar} {completed}/{total} · {orders} open orders",
        "progress_done": "all done",
        "progress_finish": "done around {when}",
        "progress_unknown_finish": "no finish time yet",
        "progress_evidence": "{count} finished in the last hour · {remaining} left",
        "progress_no_evidence": "nothing finished in the last hour · {remaining} left",
        "progress_quota": "quota {items}",
        "progress_quota_item": "{executor} {percent}% used (resets {when})",
        "progress_clock": "{hour:02d}:{minute:02d}",
        "progress_tomorrow": "tomorrow {time}",
        "progress_on_day": "{month}/{day} {time}",
    },
    "ko": {
        "progress_bar": "진행 {bar} {completed}/{total} · 열린 주문 {orders}개",
        "progress_done": "모두 끝냈어요",
        "progress_finish": "{when}쯤 끝나요",
        "progress_unknown_finish": "끝나는 시각은 아직 몰라요",
        "progress_evidence": "최근 1시간에 {count}개 끝냈어요 · {remaining}개 남았어요",
        "progress_no_evidence": "최근 1시간에 끝낸 작업이 없어요 · {remaining}개 남았어요",
        "progress_quota": "쿼터 {items}",
        "progress_quota_item": "{executor} {percent}% 썼어요({when} 리셋)",
        "progress_clock": "{hour}시 {minute:02d}분",
        "progress_tomorrow": "내일 {time}",
        "progress_on_day": "{month}월 {day}일 {time}",
    },
}

