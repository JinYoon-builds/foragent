import hashlib
from pathlib import Path

import pytest

from foragent import order_waiting
from foragent.issue import (
    ChecklistItem,
    _sections,
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
    assert parse_body(body).other_content == "Authored tail."


def test_gate_text_in_a_closed_fence_stays_authored_and_commands_keep_it():
    example = f"## Example\n```md\n## Waiting\nnot a wait\n{WRITE}\n```\nAfter the fence.\n"
    assert authored_body(example) == example.strip() == parse_body(example).other_content
    script = f"cat <<'EOF'\n## Waiting\n{WRITE}\nEOF"
    assert parse_body(f"## Done when\n```sh\n{script}\n```\n").done_when_commands == [script]


def test_marker_text_sharing_a_line_with_other_text_stays():
    body = "## Example\nThe header is<!-- foragent:v1 -->\n<!-- foragent:v1 --> and text\n"
    assert authored_body(body) == body.strip()


@pytest.mark.parametrize("trailing", ["", "  ", "\t"])
@pytest.mark.parametrize("wait", [None, "A wait"])
def test_what_the_gate_writes_after_a_body_without_final_newline_is_not_authored(trailing, wait):
    original = f"## Goal\nShip it.\n## Contracts\nThe API stays.{trailing}"
    edited = order_waiting.replace_section(original, wait) if wait else original + "\nMore."
    written = order_waiting.mark_write(original, order_waiting.stamp(original, edited))
    assert "foragent:write" in written
    assert authored_body(written) == (original.strip() if wait else edited)
    assert order_waiting.mark_write(written, order_waiting.stamp(written, edited)) == written


def test_a_body_ending_in_an_unfinished_fence_keeps_gate_writing_recognisable():
    original = "## Goal\nShip.\n\n## Example\n```\ncode\n"
    written = order_waiting.mark_write(original, order_waiting.stamp(original, order_waiting.replace_section(original, "A wait")))
    assert "A wait" in written and "foragent:write" in written and "foragent:waiting-prior" in written
    assert authored_body(written) == original.strip()
    assert parse_body(written).other_content == "## Example\n```\ncode"


def test_an_unfinished_fence_still_runs_to_the_end_for_sections():
    parsed = parse_body("## Goal\nShip.\n```\n## Done when\n```sh\nfalse\n")
    assert parsed.done_when_commands == [] and "false" in parsed.goal


def test_authored_body_ignores_where_the_gate_appended_and_line_endings():
    assert authored_body(PLAIN + "\n\n## Waiting\nA wait\n\n" + PRIOR + "\n" + WRITE + "\n") == authored_body(PLAIN)
    assert authored_body(PLAIN.replace("\n", "\r\n")) == authored_body(PLAIN)


def test_other_content_does_not_change_whether_two_bodies_parse_equal():
    assert parse_body(PLAIN + "## Contracts\nThe API stays.\n") == parse_body(PLAIN)
    assert parse_body(PLAIN + "## Contracts\nThe API stays.\n").other_content != parse_body(PLAIN).other_content
    assert parse_body(PLAIN.replace("Ship it.", "Ship more.")) != parse_body(PLAIN)


def test_sections_reader_gives_authored_lines_without_marker_lines():
    body = f"## Behaviour\nIt runs.\n{WRITE}\n## Waiting\nA wait\n"
    assert "".join(_sections(body)["Behaviour"]) == "It runs.\n"


@pytest.mark.parametrize("glued", [WRITE, PRIOR, PRIOR + WRITE, PRIOR + "\n" + WRITE, WRITE + "\n" + PRIOR])
@pytest.mark.parametrize("last", ["The API stays.", "- [ ] Looks right", "The API stays.  "])
def test_markers_glued_in_an_approved_snapshot_are_not_authored(glued, last):
    clean = f"{PLAIN}\n## Contracts\n{last}"
    body = clean + glued + "\n"
    assert authored_body(body) == authored_body(clean) == clean.replace("<!-- foragent:v1 -->\n", "").strip()
    assert parse_body(body).other_content == parse_body(clean).other_content
    assert "foragent:" not in parse_body(body).other_content


def test_glued_marker_in_goal_or_inside_an_unfinished_fence_is_not_authored():
    assert parse_body(f"## Goal\nShip it.{WRITE}\n").goal == "Ship it."
    assert authored_body(f"## Example\n```\ncode{WRITE}\n") == "## Example\n```\ncode"


def test_glued_marker_on_a_closing_fence_closes_it_like_the_own_line_form():
    glued, own = parse_body(f"## Done when\n```sh\ntrue\n```{WRITE}\n"), parse_body(f"## Done when\n```sh\ntrue\n```\n{WRITE}\n")
    assert glued.done_when_commands == own.done_when_commands == ["true"]
    assert glued.other_content == own.other_content == ""
    assert authored_body(f"## Example\n```\ncode\n```{WRITE}\n") == "## Example\n```\ncode\n```"


def test_marker_text_inside_a_closed_fence_stays_when_it_is_not_the_closing_line():
    body = f"## Example\n```\nvalue{WRITE}\n```\nEnd.\n"
    assert authored_body(body) == body.strip()


@pytest.mark.parametrize("glued", [WRITE, PRIOR, PRIOR + WRITE])
def test_a_marker_glued_to_a_fence_line_stays_out_when_more_content_follows(glued):
    clean = ("## Goal\nShip it.\n\n## Example\n```sh\none\n```\n\n## Out of scope\nOthers.\n\n"
             "## More\n```\ntwo\n```\n\n## Done when\n```sh\ntrue\n```\n")
    closing = clean.replace("one\n```\n", f"one\n```{glued}\n", 1)
    opening = clean.replace("```sh\none", f"```sh{glued}\none", 1)
    for body in (closing, opening):
        assert authored_body(body) == clean.strip()
        assert parse_body(body).out_of_scope == "Others." and parse_body(body).done_when_commands == ["true"]
        assert parse_body(body).other_content == parse_body(clean).other_content
        assert "foragent:" not in parse_body(body).other_content


def test_a_glued_command_fence_before_more_content_keeps_each_command_as_written():
    script = f"cat <<'EOF'\nvalue{WRITE}\nEOF"
    parsed = parse_body(f"## Done when\n```sh\n{script}\n```{PRIOR}\n```sh\ntrue\n```\n")
    assert parsed.done_when_commands == [script, "true"] and parsed.other_content == ""


def test_a_glued_prose_line_before_more_content_is_clean():
    assert authored_body(f"## Contracts\nThe API stays.{WRITE}\n\n## More\nText.\n") == (
        "## Contracts\nThe API stays.\n\n## More\nText.")
