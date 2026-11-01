"""`fa order` refusals when acceptance files do not collect on the base (#1523)."""

MESSAGES = {
    "en": {
        "order_collect_refused": (
            "Refused: this order's acceptance files fail collection on base {base}, the tree main would "
            "receive. Nothing was published or recorded. Files collected: {files}.\n{errors}"
        ),
        "order_collect_unavailable": (
            "Refused: the collection check is unavailable, so no result exists. Files: {files}. Command: {command}. Cause: {cause}"
        ),
        "order_collect_no_base": "cannot read the current base {branch} from origin: {error}",
        "order_collect_no_command": "the project has no test command",
        "order_collect_no_command_name": "(none)",
        "order_collect_unsupported": "the project's test command is not a pytest command, so it cannot collect",
        "order_collect_slot_timeout": "no machine test slot became free before the collection deadline",
        "order_collect_outside": "the test command reaches outside the clean copy of the base: {word}",
        "order_collect_timeout": "collection did not finish within {seconds} seconds",
        "order_collect_exit": "the collection command exited with code {code} and no collection result: {output}",
    },
    "ko": {
        "order_collect_refused": (
            "거절했어요: 이 주문의 인수 파일이 main이 받을 기준 {base}에서 수집되지 않아요. "
            "아무것도 발행하거나 기록하지 않았어요. 수집한 파일: {files}.\n{errors}"
        ),
        "order_collect_unavailable": (
            "거절했어요: 수집 검사를 할 수 없어서 결과가 없어요. 파일: {files}. 명령: {command}. 원인: {cause}"
        ),
        "order_collect_no_base": "origin에서 현재 기준 {branch}를 읽을 수 없어요: {error}",
        "order_collect_no_command": "프로젝트에 시험 명령이 없어요",
        "order_collect_no_command_name": "(없음)",
        "order_collect_unsupported": "프로젝트의 시험 명령이 pytest 명령이 아니라서 수집할 수 없어요",
        "order_collect_slot_timeout": "수집 마감 전에 기계 시험 칸이 비지 않았어요",
        "order_collect_outside": "시험 명령이 기준의 깨끗한 사본 밖을 가리켜요: {word}",
        "order_collect_timeout": "수집이 {seconds}초 안에 끝나지 않았어요",
        "order_collect_exit": "수집 명령이 코드 {code}로 끝났고 수집 결과가 없어요: {output}",
    },
}
