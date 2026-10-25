"""Status lines for the machine-wide budget from machine.yml (#365)."""

MESSAGES = {
    "en": {
        "machine_budget_line": "Machine budget (machine.yml): {values}",
        "machine_budget_value": "{name} {value} ({source})",
        "machine_budget_none": "none",
        "machine_budget_source_machine": "machine",
        "machine_budget_source_default": "default",
        "machine_budget_source_invalid": "invalid, default used",
        "machine_budget_unknown_keys": "machine.yml keys ignored as unknown: {keys}",
    },
    "ko": {
        "machine_budget_line": "기계 예산(machine.yml): {values}",
        "machine_budget_value": "{name} {value} ({source})",
        "machine_budget_none": "없음",
        "machine_budget_source_machine": "machine",
        "machine_budget_source_default": "기본값",
        "machine_budget_source_invalid": "잘못된 값, 기본값 사용",
        "machine_budget_unknown_keys": "machine.yml의 모르는 키는 무시해요: {keys}",
    },
}
