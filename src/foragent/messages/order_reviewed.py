"""Why publication refuses a statement issue's reviewed tests (#1049)."""

MESSAGES = {
    "en": {
        "reviewed_missing": (
            "cannot publish: statement issue(s) {keys} have no reviewed tests; prepare them with "
            "fa order prepare PLAN.json so the lane writes the tests and the configured reviewer "
            "reviews them, then publish again (fa order publish PLAN.json --dry-run still previews the plan)."),
        "reviewed_stale": (
            "cannot publish: the review of statement issue(s) {keys} was for other requirements, contracts, "
            "references or writer/reviewer setup than the plan has now; run fa order prepare PLAN.json "
            "so the reviewer reviews tests for the current plan."),
        "reviewed_differ": (
            "cannot publish: the tests or Done when commands of statement issue(s) {keys} differ from the "
            "reviewed ones; run fa order prepare PLAN.json so the reviewer reviews the current files."),
        "reviewed_base_moved": (
            "cannot publish: the base branch moved to {base} since the tests of statement issue(s) {keys} "
            "were reviewed; run fa order prepare PLAN.json for new red evidence on the current base, "
            "then publish again."),
    },
    "ko": {
        "reviewed_missing": (
            "발행할 수 없어요: 동작 문장 이슈 {keys}에 검토된 시험(reviewed tests)이 없어요. "
            "fa order prepare PLAN.json으로 레인이 시험을 쓰고 설정된 검토자가 검토하게 한 뒤 다시 발행하세요 "
            "(fa order publish PLAN.json --dry-run으로 계획은 미리 볼 수 있어요)."),
        "reviewed_stale": (
            "발행할 수 없어요: 동작 문장 이슈 {keys}의 검토(review)는 지금 계획과 다른 요구, 계약, 참조 또는 "
            "작성자·검토자 설정에 대한 것이에요. fa order prepare PLAN.json으로 지금 계획의 시험을 다시 검토받으세요."),
        "reviewed_differ": (
            "발행할 수 없어요: 동작 문장 이슈 {keys}의 시험이나 Done when 명령이 검토(review)된 것과 달라요. "
            "fa order prepare PLAN.json으로 지금 파일을 다시 검토받으세요."),
        "reviewed_base_moved": (
            "발행할 수 없어요: 동작 문장 이슈 {keys}의 시험을 검토(review)한 뒤 기준 브랜치가 {base}(으)로 "
            "움직였어요. fa order prepare PLAN.json으로 지금 기준에서 새 실패 증거를 받은 뒤 다시 발행하세요."),
    },
}
