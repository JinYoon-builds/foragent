"""Order parent notices: completion summary, a cancelled child and the batch PR's parent line.

``order_record_closed`` is shared by fa order publish and the gate tick.
"""

MESSAGES = {
    "en": {
        "parent_children_done": "All {count} sub-issues are done, so Foragent closed this order.",
        "parent_closed_report": "#{number} closed: all {count} sub-issues completed",
        "order_record_closed": "This order is applied and has no open sub-issues, so its record is closed.",
        "order_record_closed_report": "#{number} closed: order record with no sub-issues",
        # No closing keyword (close/fix/resolve): only the gate closes the parent.
        "batch_pr_parent": "Sub-issues of #{parent}",
        "parent_child_not_planned": (
            "#{number}: sub-issue(s) {children} closed as not planned; the order stays open for a decision."
        ),
    },
    "ko": {
        "parent_children_done": "하위 이슈 {count}개가 모두 끝나서 Foragent가 이 주문을 닫았어요.",
        "parent_closed_report": "#{number}을(를) 닫았어요. 하위 이슈 {count}개가 모두 끝났어요",
        "order_record_closed": "이 주문은 적용됐고 열린 하위 이슈가 없어서 주문 기록을 닫았어요.",
        "order_record_closed_report": "#{number}을(를) 닫았어요. 하위 이슈가 없는 주문 기록이에요",
        "batch_pr_parent": "#{parent}의 하위 이슈",
        "parent_child_not_planned": (
            "#{number}: 하위 이슈 {children}이(가) 계획 안 함으로 닫혔어요. 결정이 필요해서 주문을 열어 둬요."
        ),
    },
}
