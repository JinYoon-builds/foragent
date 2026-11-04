"""`fa order` refusals when acceptance files do not collect on the base (#1523) or break their importers."""

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
        "order_collect_timeout": "collection did not finish within {seconds} seconds",
        "order_collect_exit": "the collection command exited with code {code} and no collection result: {output}",
        "order_collect_importers_refused": (
            "Refused: this order's changed test files {files} break tests that import them and pass on base "
            "{base}. Nothing was published or recorded. Broken tests:\n{tests}"
        ),
        "order_collect_importers_unavailable": (
            "Refused: the tests that import this order's changed files {files} could not be checked, so no "
            "result exists. Tests: {tests}. Command: {command}. Cause: {cause}"
        ),
        "order_collect_importers_timeout": "the importing tests did not finish within {seconds} seconds",
        "order_collect_importers_gone": "this test passed on the base and no longer runs with this order's files",
        "order_collect_importers_incomplete": (
            "the importing tests did not finish on the base (exit code {code}), so there is nothing to compare: {output}"
        ),
        "order_collect_importers_stopped": (
            "the run with this order's files stopped before this test finished (exit code {code}):\n{output}"
        ),
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
        "order_collect_timeout": "수집이 {seconds}초 안에 끝나지 않았어요",
        "order_collect_exit": "수집 명령이 코드 {code}로 끝났고 수집 결과가 없어요: {output}",
        "order_collect_importers_refused": (
            "거절했어요: 이 주문이 바꾼 시험 파일 {files}가 그 파일을 import하는 시험을 깨요. 그 시험은 기준 "
            "{base}에서 통과해요. 아무것도 발행하거나 기록하지 않았어요. 깨진 시험:\n{tests}"
        ),
        "order_collect_importers_unavailable": (
            "거절했어요: 이 주문이 바꾼 파일 {files}를 import하는 시험을 확인할 수 없어서 결과가 없어요. "
            "시험: {tests}. 명령: {command}. 원인: {cause}"
        ),
        "order_collect_importers_timeout": "import하는 시험이 {seconds}초 안에 끝나지 않았어요",
        "order_collect_importers_gone": "이 시험은 기준에서 통과했는데 이 주문의 파일로는 더 이상 돌지 않아요",
        "order_collect_importers_incomplete": (
            "import하는 시험이 기준에서 끝나지 않아서(종료 코드 {code}) 비교할 결과가 없어요: {output}"
        ),
        "order_collect_importers_stopped": (
            "이 주문의 파일로 돌린 실행이 이 시험이 끝나기 전에 멈췄어요(종료 코드 {code}):\n{output}"
        ),
    },
}
