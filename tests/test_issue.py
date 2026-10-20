import hashlib
from pathlib import Path

import pytest

from foragent.issue import (
    ChecklistItem,
    acceptance_paths,
    body_hash,
    has_machine_check,
    parse_body,
)


FIXTURES = Path(__file__).resolve().parents[1] / "docs/impl/fixtures"


@pytest.mark.parametrize(
    "name,version,files,checklist",
    [
        ("a", "v1", ["test_a_words.py"], []),
        ("b", "v1", ["test_b_lines.py", "test_b_cli.py"], [
            ChecklistItem("The help text explains the lines command.", False),
            ChecklistItem("Empty files are covered by an acceptance test.", True),
        ]),
        ("c", None, ["test_c_errors.py"], []),
        ("d", "v1", [], [
            ChecklistItem("The README includes a words example.", False),
            ChecklistItem("The README includes a lines example.", True),
        ]),
    ],
)
def test_sandbox_fixtures(name, version, files, checklist):
    body = (FIXTURES / f"issue_{name}.md").read_text(encoding="utf-8")
    parsed = parse_body(body)
    assert parsed.version == version
    assert parsed.goal
    assert parsed.out_of_scope.startswith("- ")
    assert parsed.done_when_commands == [f"uv run pytest -q tests/acceptance/{file}" for file in files]
    assert parsed.done_when_checklist == checklist
    assert has_machine_check(parsed) is bool(files)
    assert parse_body(body.replace("\n", "\r\n")) == parsed


@pytest.mark.parametrize("body", ["", "text", "## goal\nwrong case", " ## Goal\nindented"])
def test_missing_sections(body):
    parsed = parse_body(body)
    assert parsed.version is None
    assert parsed.goal == parsed.out_of_scope == ""
    assert parsed.done_when_commands == parsed.done_when_checklist == []


def test_exact_headings_trailing_spaces_and_section_boundaries():
    parsed = parse_body(
        "preamble\n## Goal   \n First line\n\n Second line \n\n"
        "## Other\nexcluded\n## Out of scope\n- Scope\n"
        "## Done when\n```\ntrue\n```\n## Notes\n```\nfalse\n```\n"
    )
    assert parsed.goal == "First line\n\n Second line"
    assert parsed.out_of_scope == "- Scope"
    assert parsed.done_when_commands == ["true"]


@pytest.mark.parametrize("fence", ["```", "````"])
@pytest.mark.parametrize("heading", ["## acceptance checks", "## Goal", "## Done when"])
def test_headings_inside_scripts_do_not_end_sections(fence, heading):
    parsed = parse_body(
        f"## Done when\n{fence}sh\ntrue\n{fence}\n"
        f"{fence}sh\n{heading}\nfalse\n{fence}\n"
        "- [ ] outside\n## Notes\n```sh\nexcluded\n```\n"
    )
    assert parsed.done_when_commands == ["true", f"{heading}\nfalse"]
    assert parsed.done_when_checklist == [ChecklistItem("outside", False)]
    assert has_machine_check(parsed)


def test_fenced_headings_in_goal_do_not_create_sections():
    goal = "Example:\n```md\n## Done when\n## Out of scope\n```"
    parsed = parse_body(f"## Goal\n{goal}\n## Done when\n```sh\ntrue\n```\n")
    assert parsed.goal == goal
    assert parsed.out_of_scope == ""
    assert parsed.done_when_commands == ["true"]


@pytest.mark.parametrize("heading", ["## Goal extra", "## GOAL", "### Goal", "## Done when extra"])
def test_near_match_is_not_a_section(heading):
    parsed = parse_body(f"{heading}\ntext\n```sh\ntrue\n```")
    assert parsed.goal == ""
    assert parsed.done_when_commands == []


def test_scripts_keep_inner_text_and_checklists_stay_outside_fences():
    parsed = parse_body(
        "## Done when\n- [ ] outside\n```bash\n  echo 'a'  \n\n"
        "- [x] inside\n\n```\n* [x] checked\n* [X] upper\n"
        "````sh\necho '```'\n```\n- [ ] still inside\n````\n- [ ] last\n"
    )
    assert parsed.done_when_commands == [
        "  echo 'a'  \n\n- [x] inside\n",
        "echo '```'\n```\n- [ ] still inside",
    ]
    assert parsed.done_when_checklist == [
        ChecklistItem("outside", False), ChecklistItem("checked", True),
        ChecklistItem("upper", True), ChecklistItem("last", False),
    ]


def test_only_done_when_commands_count():
    parsed = parse_body("## Goal\n```sh\nfalse\n```\n## Done when\n```\ntrue\n```")
    assert parsed.done_when_commands == ["true"]


@pytest.mark.parametrize("inner", ["", "\n", "  \n\t\n"])
def test_blank_scripts_are_not_machine_checks(inner):
    parsed = parse_body(f"## Done when\n```\n{inner}```\n")
    assert not has_machine_check(parsed)


def test_incomplete_fence_is_not_a_command():
    parsed = parse_body("## Done when\n```sh\ntrue\n- [ ] still inside")
    assert parsed.done_when_commands == []
    assert parsed.done_when_checklist == []


def test_repeated_sections_use_first_occurrence():
    assert parse_body("## Goal\nfirst\n## Goal\nsecond").goal == "first"


@pytest.mark.parametrize("directory", ["tests/acceptance", "tests/acceptance/", "./tests/acceptance/"])
def test_acceptance_paths(directory):
    commands = [
        "uv run pytest ./tests/acceptance/test_b.py::TestB::test_one tests/unit/test_x.py",
        "pytest 'tests/acceptance/with spaces.py::test_two' \"./tests/acceptance/test_a.py\"",
        "pytest tests/acceptance tests/acceptance/ ./tests/acceptance/test_b.py",
        "pytest tests/acceptance_extra/test_no.py tests/acceptance.py /tests/acceptance/absolute.py",
    ]
    assert acceptance_paths(commands, directory) == [
        "tests/acceptance", "tests/acceptance/test_a.py", "tests/acceptance/test_b.py",
        "tests/acceptance/with spaces.py",
    ]


def test_acceptance_paths_custom_directory_and_empty_input():
    assert acceptance_paths(["sh './checks/smoke test.sh' checks/sub/"], "./checks") == [
        "checks/smoke test.sh", "checks/sub",
    ]
    assert acceptance_paths([], "checks/") == []


@pytest.mark.parametrize("command,expected", [
    ("pytest 'tests/acceptance/test_a.py", ["tests/acceptance/test_a.py"]),
    ('pytest "./tests/acceptance/test_a.py::test_one', ["tests/acceptance/test_a.py"]),
    ("pytest tests/acceptance/test_a.py'", ["tests/acceptance/test_a.py"]),
    ('pytest \'./tests/acceptance/test_a.py"', ["tests/acceptance/test_a.py"]),
    ("echo 'unfinished", []),
])
def test_acceptance_paths_falls_back_on_unmatched_quotes(command, expected):
    assert acceptance_paths([command], "tests/acceptance/") == expected
    assert acceptance_paths([command, "pytest tests/acceptance/test_b.py"], "tests/acceptance/") == sorted(
        expected + ["tests/acceptance/test_b.py"]
    )


def test_body_hash_known_digest_and_normalization():
    assert body_hash(" \r\nabc\r\n\t") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert body_hash("alpha\r\nbeta") == body_hash("alpha\nbeta\n")
    assert body_hash("alpha  beta") != body_hash("alpha beta")
    assert body_hash("한글") == hashlib.sha256("한글".encode("utf-8")).hexdigest()


@pytest.mark.parametrize("section", ["Goal", "Out of scope", "Done when"])
def test_body_hash_binds_all_sections(section):
    body = "## Goal\na\n## Out of scope\nb\n## Done when\n```\ntrue\n```"
    assert body_hash(body) != body_hash(body.replace(f"## {section}", f"## {section}\nchanged"))
