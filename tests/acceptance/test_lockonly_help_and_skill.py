"""Acceptance tests for lockonly-help (UX r1102).

#2558 lets an edit that changes only a published issue's existing locked acceptance files
publish without test preparation (the publication recheck proves it), and counts statements
the existing locks cover as covered when an edit is prepared. The publish help's statements
paragraph and the fa-order skill (Local preparation progress; step 6) must say so.
Every test here fails on current main; the self-check of the word matcher is in
test_lockonly_help_guard.py.
"""

from lockonly_help_words import (COVERED, LOCK_ONLY, SKILL, _publish_help, _section,
                                 _states, _statements_paragraph)


def test_publish_help_statements_paragraph_names_the_lock_only_route(capsys):
    paragraph = _statements_paragraph(_publish_help(capsys))
    assert "reviewed-tests route" in paragraph, "The existing statements paragraph stays"
    assert _states(paragraph, LOCK_ONLY), (
        "The statements paragraph of `fa order publish --help` must say that an edit changing "
        "only an issue's existing locked acceptance files publishes without test preparation, "
        f"proved by the publication recheck:\n{paragraph}")


def test_skill_preparation_progress_names_the_lock_only_route():
    section = _section(SKILL.read_text(encoding="utf-8"), "## Local preparation progress")
    assert "fa order prepare PLAN.json --status --json" in section.replace("`", "")
    assert _states(section, LOCK_ONLY), (
        "The fa-order skill's preparation-progress guidance must say that an edit changing only "
        "an issue's existing locked acceptance files publishes without test preparation, proved "
        f"by the publication recheck:\n{section}")


def test_skill_step_six_names_the_lock_only_route():
    section = _section(SKILL.read_text(encoding="utf-8"), "## 6. One approval")
    assert "fa order prepare PLAN.json --json" in section.replace("`", "")
    assert _states(section, LOCK_ONLY), (
        "Step 6 of the fa-order skill must say that an edit changing only an issue's existing "
        "locked acceptance files publishes without test preparation, proved by the publication "
        f"recheck:\n{section}")


def test_skill_says_existing_locks_count_as_covered_when_an_edit_is_prepared():
    text = SKILL.read_text(encoding="utf-8")
    sections = (_section(text, "## Local preparation progress"), _section(text, "## 6. One approval"))
    assert any(_states(section, COVERED) for section in sections), (
        "The fa-order skill (preparation progress or step 6) must say that, when an edit is "
        "prepared, statements the issue's existing locked tests already cover count as covered")
