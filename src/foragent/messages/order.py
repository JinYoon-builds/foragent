"""Order replacement messages."""

MESSAGES = {
    "en": {
        "replaces_invalid": "{field} must be a list of unique positive issue numbers.",
        "replaces_missing": "Cannot replace #{number}: the issue could not be found.",
        "replaces_edited": "Cannot replace #{number}: it is edited by this order.",
        "replaces_approved": "Cannot replace #{number}: it is a child approved by this order.",
        "replaces_parent": "Cannot replace #{number}: it is this order's parent.",
        "replaces_pr": "Cannot replace #{number}: it is a pull request.",
        "replaces_running": "Cannot replace #{number}: it is running.",
        "replaces_preview": "Would close as not planned: #{number} (replaced by {successors}).",
        "replaces_comment": "Replaced by {successors}; closing as not planned.",
    },
    "ko": {
        "replaces_invalid": "{field}에는 중복 없는 양의 이슈 번호 목록을 넣어 주세요.",
        "replaces_missing": "#{number} 이슈를 대체할 수 없어요: 이슈를 찾을 수 없어요.",
        "replaces_edited": "#{number} 이슈를 대체할 수 없어요: 같은 주문에서 수정하는 이슈예요.",
        "replaces_approved": "#{number} 이슈를 대체할 수 없어요: 같은 주문에서 승인하는 자식 이슈예요.",
        "replaces_parent": "#{number} 이슈를 대체할 수 없어요: 이 주문의 부모 이슈예요.",
        "replaces_pr": "#{number} 이슈를 대체할 수 없어요: 풀 리퀘스트예요.",
        "replaces_running": "#{number} 이슈를 대체할 수 없어요: 실행 중이에요.",
        "replaces_preview": "진행하지 않음으로 닫을 이슈: #{number} (대체: {successors}).",
        "replaces_comment": "{successors} 이슈로 대체해서 진행하지 않음으로 닫아요.",
    },
}
