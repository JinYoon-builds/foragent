"""Risk map (fa risk add / list / candidates) notices."""

MESSAGES = {
    "en": {
        "risk_added": "Added a risk for {paths} to {file}.",
        "risk_empty": "No risks recorded yet ({file}).",
        "risk_always_human": " (always a person)",
        "risk_candidates": "Found {found} risky spots; {covered} are already on the map. Unmapped files:",
        "risk_invalid_yaml": "{file} isn't valid YAML: {error}",
        "risk_not_a_list": "{file} must be a YAML list of entries.",
        "risk_entry_paths": "Entry {index} in {file} needs a non-empty list of paths.",
        "risk_blank_options": "A risk entry needs a non-blank {options}.",
        "risk_bad_date": "--incident-date must look like YYYY-MM-DD, not {value!r}.",
        "risk_guard_missing": "These guard tests don't exist: {tests}",
        "risk_append_failed": "Couldn't add the entry to {file} safely. Please fix its layout by hand.",
    },
    "ko": {
        "risk_added": "{file}에 {paths} 위험을 추가했어요",
        "risk_empty": "아직 기록한 위험이 없어요({file})",
        "risk_always_human": " (항상 사람이 봐요)",
        "risk_candidates": "위험한 곳을 {found}군데 찾았어요. 그중 {covered}군데는 이미 지도에 있어요. 지도에 없는 파일:",
        "risk_invalid_yaml": "{file}이 올바른 YAML이 아니에요: {error}",
        "risk_not_a_list": "{file}은 항목들의 YAML 목록이어야 해요",
        "risk_entry_paths": "{file}의 {index}번째 항목에 경로 목록이 비어 있어요",
        "risk_blank_options": "위험 항목에는 비어 있지 않은 {options} 값이 필요해요",
        "risk_bad_date": "--incident-date는 YYYY-MM-DD 모양이어야 해요({value!r})",
        "risk_guard_missing": "이 보호 시험 파일이 없어요: {tests}",
        "risk_append_failed": "{file}에 항목을 안전하게 추가하지 못했어요. 파일 모양을 직접 고쳐 주세요",
    },
}
