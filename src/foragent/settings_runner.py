"""The runner_unit_tests team setting as the settings window shows it (#308).

`auto` (the default) means: leave importer-only unit tests to CI when a GitHub Actions
workflow runs on pull_request, else run them all (run_checks.trusts_ci decides the same way).
The window names which of the two applies.
"""

from __future__ import annotations

from pathlib import Path


def applied(repo_root: Path | None) -> str:
    """The choice `auto` stands for in this checkout: ci or all."""
    from foragent import run_checks

    return "ci" if repo_root is not None and run_checks.has_pull_request_ci(repo_root) else "all"


def auto_text(repo_root: Path | None, language: str) -> str:
    """For example 'automatic (leave to CI)'."""
    from foragent.i18n import text

    choice = text(f"settings_window_value_runner_unit_tests_{applied(repo_root)}", language)
    return text("settings_window_runner_auto", language, choice=choice)
