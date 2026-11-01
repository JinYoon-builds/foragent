"""Planning note controls and waiting views."""

MESSAGES = {
    "en": {
        "order_notes_help": "Append a waiting decision, idea or rule outside the checkout",
        "order_notes_pending_help": "Show retained waiting orders and notes",
        "order_notes_defer_help": "Publication boundary chosen by planning",
        "order_notes_kind_help": "Kind of planning note",
        "order_notes_text_help": "Text to retain verbatim",
        "order_notes_replaces_help": "Earlier decision id superseded by this new entry",
        "order_notes_saved": "Note {id} waiting for {boundary}.",
        "order_notes_row": "{id} [{kind}] {status} for {boundary}: {text}",
        "order_notes_empty": "No waiting notes.",
        "order_notes_invalid": "Choose decision, idea or rule and supply nonempty text and boundary.",
        "order_notes_replaces_invalid": "Only a decision can replace an earlier recorded decision id.",
        "order_notes_short_write": "The note append was incomplete; retry the note.",
        "order_notes_io": "Could not read or append planning notes: {error}",
    },
    "ko": {
        "order_notes_help": "작업 폴더 밖에 대기할 결정·아이디어·규칙을 덧붙여요",
        "order_notes_pending_help": "보관된 대기 주문과 노트를 보여 줘요",
        "order_notes_defer_help": "기획에서 고른 발행 경계예요",
        "order_notes_kind_help": "기획 노트 종류예요",
        "order_notes_text_help": "그대로 보관할 내용이에요",
        "order_notes_replaces_help": "새 항목이 대체하는 이전 결정의 id예요",
        "order_notes_saved": "노트 {id}가 {boundary} 경계를 기다려요.",
        "order_notes_row": "{id} [{kind}] {boundary} {status}: {text}",
        "order_notes_empty": "대기 중인 노트가 없어요.",
        "order_notes_invalid": "decision·idea·rule 중 고르고 내용과 경계를 빈칸 없이 적어 주세요.",
        "order_notes_replaces_invalid": "결정만 이미 기록된 결정 id를 대체할 수 있어요.",
        "order_notes_short_write": "노트를 끝까지 덧붙이지 못했어요. 다시 기록해 주세요.",
        "order_notes_io": "기획 노트를 읽거나 덧붙이지 못했어요: {error}",
    },
}
