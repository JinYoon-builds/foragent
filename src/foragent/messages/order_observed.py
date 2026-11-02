"""Publication conflicts with the GitHub fields a plan last read."""

MESSAGES = {
    "en": {
        "observed_invalid": "Cannot read the observed issue base at {path}; restore it before publishing.",
        "observed_context": "Current issue on GitHub #{number}\nTitle: {title}\nBody:\n{body}",
        "observed_change": "{field}\nOld: {old}\nCurrent: {current}",
        "observed_refusal": "Publication stopped: GitHub issues changed since this plan read them.\n{changes}\nRead the current issues and run fa order preread PLAN.json again before publishing.",
        "observed_field_title": "Title",
        "observed_field_body": "Body",
        "observed_field_version": "Body version",
        "observed_field_goal": "Goal",
        "observed_field_out_of_scope": "Out of scope",
        "observed_field_done_when_commands": "Done when",
        "observed_field_done_when_checklist": "Done when checklist",
        "observed_field_files": "Files",
        "observed_field_exclusive": "Exclusive files",
    },
    "ko": {
        "observed_invalid": "{path}에서 읽어 둔 이슈 기준을 확인할 수 없어요. 복구한 뒤 발행해 주세요.",
        "observed_context": "GitHub의 현재 이슈 #{number}\n제목: {title}\n본문:\n{body}",
        "observed_change": "{field}\n이전: {old}\n현재: {current}",
        "observed_refusal": "읽은 뒤 GitHub 이슈가 바뀌어 발행을 멈췄어요.\n{changes}\n현재 이슈를 확인하고 fa order preread PLAN.json으로 다시 읽은 뒤 발행해 주세요.",
        "observed_field_title": "제목 (title)",
        "observed_field_body": "본문",
        "observed_field_version": "본문 버전",
        "observed_field_goal": "목표",
        "observed_field_out_of_scope": "제외 범위",
        "observed_field_done_when_commands": "완료 조건 (Done when)",
        "observed_field_done_when_checklist": "완료 점검표 (Done when checklist)",
        "observed_field_files": "변경 파일",
        "observed_field_exclusive": "독점 파일",
    },
}
