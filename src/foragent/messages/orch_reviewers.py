"""`fa orch reviewers`: per-issue limit on simultaneous risk-map reviewers."""

MESSAGES = {
    "en": {
        "orch_reviewers_usage": "fa orch reviewers: give a reviewer count or --clear, not both.",
        "orch_reviewers_invalid": "fa orch reviewers: the count must be 1 or more (got {count}); "
                                  "the issue keeps {current} reviewer(s).",
        "orch_reviewers_set": "#{issue}: up to {count} reviewer(s) at once check risk-map PRs; every perspective still gets one.",
        "orch_reviewers_cleared": "#{issue}: override cleared; up to {count} reviewer(s) at once from .foragent.yml.",
    },
    "ko": {
        "orch_reviewers_usage": "fa orch reviewers: 리뷰어 수나 --clear 중 하나만 주세요.",
        "orch_reviewers_invalid": "fa orch reviewers: 리뷰어 수는 1 이상이어야 해요(받은 값 {count}). "
                                  "이 이슈는 리뷰어 {current}명을 유지해요.",
        "orch_reviewers_set": "#{issue}: 위험 경로 PR을 리뷰어가 한 번에 최대 {count}명씩 검토해요. 관점마다 리뷰어가 한 명씩 붙어요.",
        "orch_reviewers_cleared": "#{issue}: 이슈별 설정을 지웠어요. .foragent.yml대로 한 번에 최대 {count}명씩 검토해요.",
    },
}
