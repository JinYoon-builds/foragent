"""Order parent notices: completion summary, a cancelled child and the batch PR's parent line.

``order_record_closed`` is the one closing comment for an order record, shared by
fa order publish (an edit-only order, count 0) and the tick (no sub-issue open).
The tick appends the completed and cancelled groups that are not empty.
"""

MESSAGES = {
    "en": {
        "parent_closed_report": "#{number} closed: every sub-issue ended ({completed} completed, {cancelled} cancelled)",
        "order_record_closed": "This order is applied and has no open sub-issues ({count} ended), so its record is closed.",
        "order_closed_completed": "Completed: {children}.",
        "order_closed_cancelled": "Cancelled (not planned): {children}.",
        # No closing keyword (close/fix/resolve): only the gate closes the parent.
        "batch_pr_parent": "Sub-issues of #{parent}",
        "parent_child_not_planned": (
            "#{number}: sub-issue(s) {children} closed as not planned; the order stays open for a decision."
        ),
    },
    "ko": {
        "parent_closed_report": "#{number}을(를) 닫았어요. 하위 이슈가 모두 끝났어요(완료 {completed}개, 취소 {cancelled}개)",
        "order_record_closed": "이 주문은 적용됐고 열린 하위 이슈가 없어서(끝남 {count}개) 주문 기록을 닫았어요.",
        "order_closed_completed": "완료: {children}.",
        "order_closed_cancelled": "취소(계획 안 함): {children}.",
        "batch_pr_parent": "#{parent}의 하위 이슈",
        "parent_child_not_planned": (
            "#{number}: 하위 이슈 {children}이(가) 계획 안 함으로 닫혔어요. 결정이 필요해서 주문을 열어 둬요."
        ),
    },
}
