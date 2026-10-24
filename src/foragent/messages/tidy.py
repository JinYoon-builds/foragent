"""Tidy cycle (fa tidy due / done) notices."""

MESSAGES = {
    "en": {
        "tidy_due": "Tidying is due ({reason}): {merges} merges since the last tidy ({last}).",
        "tidy_not_due": "Tidying isn't due yet: {merges} merges since the last tidy ({last}).",
        "tidy_never": "never",
        "tidy_done": "Tidy recorded at {at}. The merge count starts again.",
    },
    "ko": {
        "tidy_due": "정리할 때가 됐어요({reason}). 지난 정리 뒤 머지가 {merges}건이에요(마지막 정리: {last})",
        "tidy_not_due": "아직 정리할 때가 아니에요. 지난 정리 뒤 머지가 {merges}건이에요(마지막 정리: {last})",
        "tidy_never": "없음",
        "tidy_done": "{at}에 정리를 기록했어요. 머지 수를 처음부터 다시 셀게요",
    },
}
