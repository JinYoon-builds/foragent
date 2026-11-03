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
            "cannot publish: publication would land on {base}, not on the base the tests of statement "
            "issue(s) {keys} were reviewed on ({branch} moved, or local {branch} has commits that are not "
            "on origin; push or drop those first); run fa order prepare PLAN.json for new red evidence "
            "on that base, then publish again."),
        "reviewed_alone": (
            "cannot publish: the tests of statement issue(s) {keys} were not reviewed beside the other "
            "statement issues' reviewed tests that are published with them; run fa order prepare PLAN.json "
            "so they are reviewed together."),
        "reviewed_bytes_changed": (
            "cannot publish: {paths} would be published for statement issue(s) {keys} with other bytes "
            "than the reviewer checked (a test, helper, spec or reference publication writes changed "
            "after review); restore the reviewed file, or run fa order prepare PLAN.json so the "
            "reviewer reviews the current one."),
        "reviewed_route_kept": (
            "cannot {action}: issue(s) {keys} were prepared or published with behaviour statements, so "
            "they keep the reviewed-tests route and cannot fall back to the legacy preread; restore "
            "their statements (clarified by planning if the plan changed), then run fa order prepare "
            "PLAN.json for a fresh review."),
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
            "발행할 수 없어요: 발행은 {base} 위에 얹히는데, 동작 문장 이슈 {keys}의 시험은 다른 기준에서 "
            "검토(review)됐어요({branch}가 움직였거나, 로컬 {branch}에 origin에 없는 커밋이 있어요. 그 커밋은 "
            "먼저 push하거나 빼세요). fa order prepare PLAN.json으로 그 기준에서 새 실패 증거를 받은 뒤 "
            "다시 발행하세요."),
        "reviewed_alone": (
            "발행할 수 없어요: 동작 문장 이슈 {keys}의 시험은 함께 발행되는 다른 동작 문장 이슈의 검토된 시험과 "
            "함께 검토(review)되지 않았어요. fa order prepare PLAN.json으로 함께 검토받으세요."),
        "reviewed_bytes_changed": (
            "발행할 수 없어요: {paths} 파일이 동작 문장 이슈 {keys}에 검토자가 확인한 것과 다른 내용으로 "
            "발행될 거예요(발행이 쓰는 시험·도우미·스펙·참조 파일이 검토 뒤에 바뀌었어요). 검토된 파일로 "
            "되돌리거나 fa order prepare PLAN.json으로 지금 파일을 다시 검토받으세요."),
        "reviewed_route_kept": (
            "{action} 할 수 없어요: 이슈 {keys}는 동작 문장으로 준비되었거나 발행되었어요. 그래서 검토된 "
            "시험 경로(reviewed-tests)를 유지하고 옛 preread 경로로 돌아갈 수 없어요. 동작 문장을 되돌리고"
            "(계획이 바뀌었으면 계획에서 다듬은 문장으로) fa order prepare PLAN.json으로 새로 검토받으세요."),
    },
}
