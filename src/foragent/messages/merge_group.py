"""Lines for merge groups (#459): one joint check, each member merges its own PR."""

MESSAGES = {
    "en": {
        "merge_group_closed": "Merged in a merge group with {others}.",
        "merge_group_merged": "PR #{pr}: merged {head} in a merge group with {others}",
    },
    "ko": {
        "merge_group_closed": "{others}와(과) 함께 병합 그룹으로 병합했어요.",
        "merge_group_merged": "PR #{pr}: 병합 그룹({others})과 함께 {head}를 병합했어요",
    },
}
