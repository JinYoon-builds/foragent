"""The test-harness hold (#466)."""

MESSAGES = {
    "en": {
        "harness_changed_summary": "This PR changes the test harness outside ## Files",
        "harness_changed_paths": "A person needs to look at these test-harness paths before any test result counts:",
        "harness_unreadable": "(the change could not be read: {detail})",
        "harness_not_published": "The branch or PR could not be published: {detail}",
    },
    "ko": {
        "harness_changed_summary": "이 PR이 ## Files 밖의 시험 설정을 바꿔요",
        "harness_changed_paths": "시험 결과를 믿기 전에 사람이 이 시험 설정 경로를 봐야 해요:",
        "harness_unreadable": "(변경을 읽지 못했어요: {detail})",
        "harness_not_published": "브랜치나 PR을 올리지 못했어요: {detail}",
    },
}
