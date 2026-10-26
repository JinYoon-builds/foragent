"""Order parent notices: completion summary and a cancelled child."""

MESSAGES = {
    "en": {
        "parent_children_done": "All {count} sub-issues are done, so Foragent closed this order.",
        "parent_closed_report": "#{number} closed: all {count} sub-issues completed",
        "parent_child_not_planned": (
            "#{number}: sub-issue(s) {children} closed as not planned; the order stays open for a decision."
        ),
    },
    "ko": {
        "parent_children_done": "하위 이슈 {count}개가 모두 끝나서 Foragent가 이 주문을 닫았어요.",
        "parent_closed_report": "#{number}을(를) 닫았어요. 하위 이슈 {count}개가 모두 끝났어요",
        "parent_child_not_planned": (
            "#{number}: 하위 이슈 {children}이(가) 계획 안 함으로 닫혔어요. 결정이 필요해서 주문을 열어 둬요."
        ),
    },
}
