"""Review perspectives settings (.foragent.yml review_perspectives / review_reviewers) errors."""

MESSAGES = {
    "en": {
        "review_perspectives_shape": "review_perspectives must be a list of {{name, focus}} entries with non-blank text.",
        "review_perspectives_repeat": "review_perspectives names {name!r} more than once.",
        "review_reviewers_shape": "review_reviewers must be a whole number of 1 or more.",
    },
    "ko": {
        "review_perspectives_shape": "review_perspectives는 비어 있지 않은 {{name, focus}} 항목의 목록이어야 해요",
        "review_perspectives_repeat": "review_perspectives에 {name!r} 이름이 두 번 이상 있어요",
        "review_reviewers_shape": "review_reviewers는 1 이상의 정수여야 해요",
    },
}
