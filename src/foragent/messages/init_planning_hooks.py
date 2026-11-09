"""Planning-session hooks `fa init --decision-hooks yes` installs (r1315)."""

MESSAGES = {
    "en": {
        "planning_memory_write_reminder": (
            "You are writing a memory file. Put what belongs to FA there first, then keep only a link here: "
            "a decision with `fa order note --kind decision`, an 'if X then Y' with `fa orch promise add`, "
            "an order idea with `fa order note --kind idea`."
        ),
        "planning_hooks_settings": (
            "Cannot install the memory-write reminder in {path}: expected a settings object with hook lists."
        ),
    },
    "ko": {
        "planning_memory_write_reminder": (
            "기억 파일을 쓰려고 해요. FA가 알아야 할 것은 먼저 FA에 넣고, 여기에는 링크만 남기세요: "
            "결정은 `fa order note --kind decision`, 'X이면 Y'는 `fa orch promise add`, "
            "주문 아이디어는 `fa order note --kind idea`로 넣어요."
        ),
        "planning_hooks_settings": (
            "{path}에 기억 쓰기 알림 훅을 설치할 수 없어요. 훅 목록을 담은 설정 객체가 필요해요"
        ),
    },
}
