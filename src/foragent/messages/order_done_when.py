"""Refusals for edits that lose Done when lines. ``{line}`` is the exact issue text."""

MESSAGES = {
    "en": {
        "order_done_when_unexplained": "#{number}: this edit removes a Done when line without a reason: {line}",
        "order_done_when_blank_reason": "#{number}: this removal needs a nonblank reason: {line}",
        "order_done_when_unknown": "#{number}: this removal names a line absent from the current Done when: {line}",
        "order_done_when_kept": "#{number}: this removal names a line the edit still keeps: {line}",
        "order_done_when_hint": (
            "Keep each line, or name it in edits[].done_when_removals with a reason. "
            "`fa order edit NUMBER --json` prints the issue's current sections."
        ),
        "order_edit_help": "Print one edits[] entry from an issue's current sections",
    },
    "ko": {
        "order_done_when_unexplained": "#{number}: 이 수정은 Done when 줄을 이유 없이 지워요: {line}",
        "order_done_when_blank_reason": "#{number}: 이 제거에는 비어 있지 않은 이유가 필요해요: {line}",
        "order_done_when_unknown": "#{number}: 이 제거가 지금 Done when에 없는 줄을 가리켜요: {line}",
        "order_done_when_kept": "#{number}: 이 제거가 수정에 그대로 남은 줄을 가리켜요: {line}",
        "order_done_when_hint": (
            "각 줄을 그대로 두거나, edits[].done_when_removals에 이유와 함께 적어 주세요. "
            "`fa order edit NUMBER --json`이 이슈의 지금 구역을 보여 줘요."
        ),
        "order_edit_help": "이슈의 지금 구역으로 edits[] 항목 하나를 출력해요",
    },
}
