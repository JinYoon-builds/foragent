"""Done when executable preflight diagnostics."""

MESSAGES = {
    "en": {
        "order_executable_missing": "{issue}: missing executable {executable}: {command}{suggestion}",
        "order_executable_suggestion": "; try {command}",
        "order_executable_unknown": "; the name may be wrong",
    },
    "ko": {
        "order_executable_missing": "{issue}: 실행 파일 {executable}을 찾을 수 없어요: {command}{suggestion}",
        "order_executable_suggestion": "; {command} 형태를 써 보세요",
        "order_executable_unknown": "; 이름이 맞는지 확인해 보세요",
    },
}
