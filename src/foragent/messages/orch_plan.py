"""`fa orch pause|resume|plan`: the per-repository plan."""

MESSAGES = {
    "en": {
        "orch_plan_refused": "fa orch {action}: {reason}. Nothing changed.",
        "orch_plan_unknown_lanes": "fa orch {action}: unknown lane(s): {lanes}. Known lanes: {known}.",
        "orch_plan_bad_time": "fa orch {action}: '{value}' is not an ISO time.",
        "orch_plan_bad_width": "fa orch plan --width: '{value}' must be LANE=N (N >= 1) or LANE=off; "
                               "to stop a lane, use fa orch pause.",
        "orch_plan_bad_overlap": "fa orch plan --overlap: '{value}' must be PATH=N (N >= 1) or PATH=off.",
        "orch_plan_why_without_overlap": "fa orch plan: --why goes with --overlap; record other reasons "
                                         "with fa orch note.",
        "orch_plan_overlap_and_width": "fa orch plan: set --overlap and --width in separate commands; "
                                       "each has its own --expires.",
        "orch_plan_file": "file:{path} overlap {limit} until {until} ({by}): {why}",
        "orch_plan_file_no_expiry": "changed (no expiry)",
        "orch_plan_paused": "lane:{lane} paused until {until} ({by}): {why}",
        "orch_plan_resumed": "lane:{lane} resumed.",
        "orch_plan_not_paused": "lane:{lane} has no plan pause.",
        "orch_plan_none": "No plan; lanes follow the defaults.",
        "orch_plan_line": "lane:{lane} {parts}",
        "orch_plan_queue": "queue {order} ({by})",
        "orch_plan_pause": "paused until {until} ({by}): {why}",
        "orch_plan_width": "width {width} ({by})",
        "orch_plan_expires": "Plan expires at {at} ({by}).",
        "orch_plan_no_expiry": "Plan has no expiry; it stays until changed.",
    },
    "ko": {
        "orch_plan_refused": "fa orch {action}: {reason}. 아무것도 바꾸지 않았어요.",
        "orch_plan_unknown_lanes": "fa orch {action}: 모르는 레인이에요: {lanes}. 있는 레인: {known}.",
        "orch_plan_bad_time": "fa orch {action}: '{value}'는 ISO 시각이 아니에요.",
        "orch_plan_bad_width": "fa orch plan --width: '{value}'는 LANE=N(N은 1 이상) 또는 LANE=off여야 해요. "
                               "레인을 멈추려면 fa orch pause를 쓰세요.",
        "orch_plan_bad_overlap": "fa orch plan --overlap: '{value}'는 PATH=N(N은 1 이상) 또는 PATH=off여야 해요.",
        "orch_plan_why_without_overlap": "fa orch plan: --why는 --overlap과 함께 써요. 다른 이유는 "
                                         "fa orch note로 남기세요.",
        "orch_plan_overlap_and_width": "fa orch plan: --overlap과 --width는 따로 실행하세요. "
                                       "--expires도 각각 정해요.",
        "orch_plan_file": "file:{path} 겹침 {limit}, {until}까지({by}): {why}",
        "orch_plan_file_no_expiry": "바꿀 때",
        "orch_plan_paused": "lane:{lane}을 {until}까지 멈췄어요({by}): {why}",
        "orch_plan_resumed": "lane:{lane}을 다시 시작했어요.",
        "orch_plan_not_paused": "lane:{lane}에는 계획 멈춤이 없어요.",
        "orch_plan_none": "계획이 없어요. 레인은 기본값을 따라요.",
        "orch_plan_line": "lane:{lane} {parts}",
        "orch_plan_queue": "대기열 {order}({by})",
        "orch_plan_pause": "{until}까지 멈춤({by}): {why}",
        "orch_plan_width": "폭 {width}({by})",
        "orch_plan_expires": "계획은 {at}에 끝나요({by}).",
        "orch_plan_no_expiry": "계획에 끝나는 시각이 없어요. 바꿀 때까지 유지돼요.",
    },
}
