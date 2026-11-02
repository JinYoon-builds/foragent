"""Expected change size in preread and `fa order sizes` (#1696)."""

MESSAGES = {
    "en": {
        "sizes_help": "Show merged issues' expected and real change size and the share of wrong estimates",
        "size_unknown": "  size: unknown",
        "size_known": "  size: source={source_lines} tests={test_lines} files={file_count}",
        "size_split": "Above the {label} limit ({counts}); consider a split. You can still publish and approve.",
        "sizes_row": "#{issue} (PR #{pr}): expected {expected}, actual {actual} (source/tests/files)",
        "sizes_unknown": "unknown",
        "sizes_summary": "Wrong estimates: {wrong} of {compared} compared ({share}); {unknown} without an estimate",
        "config_size_limits_invalid": (
            "size_limits must map s, m and l to source_lines, test_lines and file_count, "
            "each a whole number of at least 0"
        ),
    },
    "ko": {
        "sizes_help": "머지된 이슈의 예상 크기와 실제 크기, 틀린 예상의 비율을 보여 줘요",
        "size_unknown": "  크기: 모름",
        "size_known": "  크기: 소스={source_lines} 시험={test_lines} 파일={file_count}",
        "size_split": "{label} 한도를 넘어요({counts}). 나누는 걸 생각해 보세요. 발행과 승인은 그대로 할 수 있어요.",
        "sizes_row": "#{issue} (PR #{pr}): 예상 {expected}, 실제 {actual} (소스/시험/파일)",
        "sizes_unknown": "모름",
        "sizes_summary": "틀린 예상: 비교한 {compared}개 중 {wrong}개({share}), 예상 없는 것 {unknown}개",
        "config_size_limits_invalid": (
            "size_limits는 s, m, l마다 source_lines, test_lines, file_count를 0 이상의 정수로 적어야 해요"
        ),
    },
}
