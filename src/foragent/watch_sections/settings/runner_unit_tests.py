"""The runner_unit_tests team row in the settings window (#308).

`auto` (the default) leaves importer-only unit tests to CI when a GitHub Actions
workflow runs on pull_request, and runs them all otherwise (run_checks.trusts_ci
decides the same way). The row names which of the two applies, for example
'automatic (leave to CI)'. ci and all use the window's usual value words.
"""

from __future__ import annotations

from pathlib import Path

KEY = "runner_unit_tests"


def applied(repo_root: Path | None) -> str:
    """The choice `auto` stands for in this checkout: ci or all."""
    from foragent import run_checks

    return "ci" if repo_root is not None and run_checks.has_pull_request_ci(repo_root) else "all"


def value_text(value, repo_root: Path | None, language: str) -> str | None:
    """The row's value for auto; None lets the window use its usual word."""
    if value != "auto":
        return None
    from foragent.i18n import text

    choice = text(f"settings_window_value_{KEY}_{applied(repo_root)}", language)
    return text("settings_window_runner_auto", language, choice=choice)
