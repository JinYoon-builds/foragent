import hashlib
from pathlib import Path

import pytest

from foragent.issue import (
    ChecklistItem,
    acceptance_paths,
    authored_body,
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


@pytest.mark.parametrize("command", [
    "pytest 'tests/acceptance/test_api.py::test_login[token=valid]'",
    "pytest --file='tests/acceptance/test_api.py::test_login[token=valid]'",
    "pytest './tests/acceptance/test_api.py::TestLogin::test_login[a=b=c]'",
])
def test_acceptance_paths_preserves_parametrized_node_selectors(command):
    assert acceptance_paths([command], "tests/acceptance/") == ["tests/acceptance/test_api.py"]


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


PLAIN = "<!-- foragent:v1 -->\n## Goal\nShip it.\n\n## Out of scope\nOthers.\n\n## Done when\n```sh\ntrue\n```\n"
WRITE = "<!-- foragent:write id=" + "a" * 32 + " -->"
PRIOR = "<!-- foragent:waiting-prior sha256=" + "b" * 64 + " id=" + "c" * 32 + " -->"


def test_plain_issue_has_no_other_content():
    assert parse_body(PLAIN).other_content == ""
    assert parse_body(PLAIN + WRITE + "\n").other_content == ""


def test_other_content_keeps_everything_the_prompts_do_not_already_state():
    parsed = parse_body(
        "<!-- foragent:v1 -->\nexclusive:\n- a.py\n\n## Goal\nShip it.\n\n## Behaviour\nIt ships.\n\n"
        "## Goal\nA second goal.\n\n## Out of scope\nOthers.\n\n## Done when\n"
        "See [the guide](docs/a.md).\n```sh\ntrue\n```\n- [ ] Looks right\n- [x] Reads well\n\n"
        "## Files\n- a.py\n\n## Contracts\nThe API stays.\n"
    )
    assert parsed.done_when_commands == ["true"] and parsed.exclusive == ["a.py"] and parsed.files == ["a.py"]
    assert parsed.other_content == (
        "exclusive:\n- a.py\n\n## Behaviour\nIt ships.\n\n## Goal\nA second goal.\n\n## Done when\n"
        "See [the guide](docs/a.md).\n- [ ] Looks right\n- [x] Reads well\n\n"
        "## Files\n- a.py\n\n## Contracts\nThe API stays."
    )


def test_unclosed_done_when_fence_is_not_a_command_but_stays_authored():
    parsed = parse_body("## Goal\nG\n## Done when\n```sh\nnever closed\n")
    assert parsed.done_when_commands == []
    assert parsed.other_content == "## Done when\n```sh\nnever closed"


@pytest.mark.parametrize("end", ["# Top heading\n", "## Contracts\n"])
def test_waiting_and_markers_are_gate_writing_but_what_follows_is_authored(end):
    body = (f"{PRIOR}\n## Goal\nShip it.\n{WRITE}\n## Waiting\nReason: bookkeeping\n"
            f"{end}Authored after.\n<!-- foragent:plan-child id=x key=y -->\n<!-- /foragent:risk-notes -->\n")
    assert authored_body(body) == f"## Goal\nShip it.\n{end}Authored after."
    assert parse_body(body).goal == "Ship it."


def test_waiting_ended_by_a_marker_resumes_authored_text():
    body = f"## Goal\nShip it.\n## Waiting\nbookkeeping\n{PRIOR}\nAuthored tail.\n"
    assert authored_body(body) == "## Goal\nShip it.\nAuthored tail."
    assert parse_body(body).goal == "Ship it."
    assert parse_body(body).other_content == "Authored tail."


FENCES = [("```md", "```"), ("````", "````"), ("`````", "`````"), ("``````md", "```````"),
          ("~~~", "~~~"), ("~~~~md", "~~~~~"), ("  ~~~", "~~~")]


@pytest.mark.parametrize("opening,closing", FENCES)
def test_fenced_gate_text_stays_authored_and_its_edits_are_seen(opening, closing):
    def fenced(example):
        return (f"## Goal\nShip it.\n\n## Example\n{opening}\n## Waiting\n{example}\n<!-- foragent:v1 -->\n"
                f"{WRITE}\nvalue{WRITE}\n{closing}\nAfter the fence.\n")

    body = fenced("not a wait")
    expected = body.split("\n\n", 1)[1].strip()
    assert parse_body(body).other_content == expected
    assert authored_body(body) == body.strip()
    assert authored_body(fenced("edited")) != authored_body(body)


@pytest.mark.parametrize("opening,inner,closer", [
    ("~~~", "```", "~~~"), ("~~~~", "~~~", "~~~~~"), ("````", "```\n~~~~", "`````"),
])
def test_only_the_fence_that_opened_closes_a_block(opening, inner, closer):
    body = f"## Example\n{opening}\n{inner}\n## Waiting\nkept\n{closer}\n## Waiting\ngone\n"
    assert authored_body(body) == f"## Example\n{opening}\n{inner}\n## Waiting\nkept\n{closer}"


def test_shorter_or_other_fence_character_does_not_close_a_long_fence():
    body = "## Example\n`````\n```\n~~~~~\n<!-- foragent:v1 -->\n`````\n<!-- foragent:v1 -->\n"
    assert authored_body(body) == "## Example\n`````\n```\n~~~~~\n<!-- foragent:v1 -->\n`````"


def test_prose_and_indented_mentions_stay_authored():
    body = f"## Example\nMentions <!-- foragent:v1 --> in prose.\n    <!-- foragent:v1 -->\n\t{WRITE}\n"
    assert authored_body(body) == body.strip()
    assert parse_body(body).other_content == body.strip()


def test_a_fence_left_open_in_waiting_does_not_hide_the_sections_after_it():
    body = "## Goal\nShip it.\n## Waiting\n```\nbookkeeping\n## Contracts\nThe API stays.\n"
    assert authored_body(body) == "## Goal\nShip it.\n## Contracts\nThe API stays."
    assert parse_body(body).other_content == "## Contracts\nThe API stays."


@pytest.mark.parametrize("fence", ["~~~", "`````"])
def test_wider_fence_recognition_does_not_turn_text_into_commands_or_sections(fence):
    parsed = parse_body(f"## Done when\n{fence}\nnot a command\n{fence}\n```sh\ntrue\n```\n- [ ] Looks right\n")
    assert parsed.done_when_commands == ["true"]
    assert [item.text for item in parsed.done_when_checklist] == ["Looks right"]
    assert parsed.other_content == f"## Done when\n{fence}\nnot a command\n{fence}\n- [ ] Looks right"


def test_commands_read_the_source_where_the_two_fence_rules_disagree():
    # The tilde fence closes first, so the marker is gate writing to the prompts but script text to the runner.
    parsed = parse_body("## Done when\n~~~\n```sh\n~~~\n<!-- foragent:v1 -->\ntrue\n```\n")
    assert parsed.done_when_commands == ["~~~\n<!-- foragent:v1 -->\ntrue"]


def test_commands_keep_gate_text_exactly_as_they_will_execute():
    script = f"cat <<'EOF'\nvalue{WRITE}\n## Waiting\n<!-- foragent:v1 -->\n{PRIOR}\nEOF"
    parsed = parse_body(f"## Done when\n```sh\n{script}\n```\n## Contracts\nThe API stays.\n")
    assert parsed.done_when_commands == [script]
    assert parsed.other_content == "## Contracts\nThe API stays."


@pytest.mark.parametrize("marker", [WRITE, PRIOR, PRIOR + "\n" + WRITE])
def test_marker_stamped_after_a_body_without_final_newline_leaves_the_authored_line(marker):
    body = f"## Goal\nShip it.\n## Contracts\nThe API stays.{marker}\n"
    assert authored_body(body) == "## Goal\nShip it.\n## Contracts\nThe API stays."


def test_marker_stamped_after_a_closing_fence_changes_no_command_and_does_not_leak():
    parsed = parse_body(f"## Done when\n```sh\ntrue\n```{WRITE}\n")
    assert parsed.done_when_commands == []
    assert parsed.other_content == "## Done when\n```sh\ntrue\n```"
    assert authored_body(f"## Example\n~~~\nx\n~~~{WRITE}\n") == "## Example\n~~~\nx\n~~~"


@pytest.mark.parametrize("marker", [WRITE, PRIOR])
def test_marker_text_before_the_body_tail_is_authored_and_its_edits_are_seen(marker):
    other = marker.replace("a" * 32, "d" * 32).replace("c" * 32, "d" * 32)
    for shape in ("## Example\n```md{m}\ncode\n```\nEnd.\n",     # fence opening info string
                  "## Example\n~~~sh{m}\ncode\n~~~\nEnd.\n",
                  "## Example\nProse ending in {m}\nEnd.\n",      # prose suffix
                  "## Example\n- [ ] item {m}\nEnd.\n"):
        body = shape.format(m=marker)
        assert authored_body(body) == body.strip()
        assert authored_body(shape.format(m=other)) != authored_body(body)


@pytest.mark.parametrize("marker", [WRITE, PRIOR])
@pytest.mark.parametrize("closing", ["~~~", "```", "````"])
def test_marker_suffix_inside_a_fence_does_not_close_it(closing, marker):
    other = "~~~" if closing[0] == "~" else "`" * len(closing)
    body = f"## Example\n{other}\n{closing}{marker}\n## Waiting\nkept\n<!-- foragent:v1 -->\n{other}\nEnd.\n"
    assert authored_body(body) == body.strip()
    assert parse_body(body).other_content == body.strip()


def test_only_markers_the_gate_appends_are_removed_from_the_body_tail():
    body = "## Example\nThe header is<!-- foragent:v1 -->\n"
    assert authored_body(body) == body.strip()


def test_gate_tail_is_removed_only_where_the_gate_writes_it():
    body = f"## Contracts\nLiteral {WRITE}\nThe API stays.{PRIOR}\n\n{WRITE}\n\n"
    assert authored_body(body) == f"## Contracts\nLiteral {WRITE}\nThe API stays."


def test_authored_body_ignores_where_the_gate_appended_and_line_endings():
    assert authored_body(PLAIN + "\n\n## Waiting\nA wait\n\n" + PRIOR + "\n" + WRITE + "\n") == authored_body(PLAIN)
    assert authored_body(PLAIN.replace("\n", "\r\n")) == authored_body(PLAIN)
