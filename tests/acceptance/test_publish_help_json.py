"""`fa order publish --help` prints a PLAN.json example that is valid JSON (backlog N71).

Public contract: the complete JSON object printed after "PLAN.json schema" in
`fa order publish --help` parses with ``json.loads`` exactly as printed (no manual edits),
and the object uses the plan, issue, edit, closure and reference-file field names and value
types the current plan validator accepts.

Placeholder content is not a schema error: an image's base64 text is a placeholder for real
bytes, so the shape check swaps only that string (keeping its type and key) for real encoded
bytes before validating. Every other value is validated exactly as printed.
"""

import base64
import copy
import json

import pytest

from foragent import cli, order
from foragent.config import Config

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _help(capsys) -> str:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["order", "publish", "--help"])
    assert exit_info.value.code == 0, "fa order publish --help must exit with code 0"
    return capsys.readouterr().out


def _example_text(help_text: str) -> str:
    """The printed object: from the first '{' after the schema heading to its matching '}'.

    Braces are matched with JSON string awareness on the printed characters, so the slice
    is the object exactly as a reader copies it, without any repair.
    """
    start = help_text.index("{", help_text.index("PLAN.json"))
    depth, inside, escaped = 0, False, False
    for index in range(start, len(help_text)):
        char = help_text[index]
        if inside:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                inside = False
        elif char == '"':
            inside = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return help_text[start:index + 1]
    raise AssertionError("The publish help must print a complete JSON object after 'PLAN.json'")


def _parsed(capsys) -> dict:
    text = _example_text(_help(capsys))
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        line = text.splitlines()[exc.lineno - 1] if exc.lineno <= len(text.splitlines()) else ""
        raise AssertionError(
            f"The PLAN.json example printed by fa order publish --help is not valid JSON: {exc} "
            f"(printed line {exc.lineno}: {line!r})") from None
    assert isinstance(value, dict), "The printed example must be one JSON object"
    return value


def test_printed_plan_example_parses_as_json_without_edits(capsys):
    value = _parsed(capsys)
    # The reference-file strings are part of the copied object and must survive as written.
    references = value.get("reference_files")
    assert isinstance(references, dict) and references, "The example still illustrates reference_files"
    texts = [content for content in references.values() if isinstance(content, str)]
    assert texts and all(content.strip() for content in texts), (
        "The example keeps a nonempty text reference file, parsed from the printed string")


def test_printed_plan_example_has_every_illustrated_section(capsys):
    value = _parsed(capsys)
    for key in ("id", "title", "summary", "spec_markdown", "issues", "edits", "closes", "reference_files"):
        assert key in value, f"The example must still illustrate plan field {key!r}"
    assert value["issues"] and isinstance(value["issues"][0], dict), "Illustrate one issue"
    assert value["edits"] and isinstance(value["edits"][0], dict), "Illustrate one edit"
    assert value["closes"] and all(isinstance(item, dict) for item in value["closes"]), "Illustrate closes"
    assert value["reference_files"], "Illustrate reference files"
    issue, edit = value["issues"][0], value["edits"][0]
    for key in ("key", "title", "goal", "out_of_scope", "done_when", "lane", "size"):
        assert key in issue, f"The example issue must illustrate {key!r}"
    for key in ("number", "goal", "out_of_scope", "done_when"):
        assert key in edit, f"The example edit must illustrate {key!r}"
    forms = {frozenset(item) for item in value["closes"]}
    assert frozenset({"number", "commit"}) in forms and frozenset({"number", "into"}) in forms, (
        "The example closes must show both the commit and the into form")


def test_printed_plan_example_matches_the_plan_validator(capsys, tmp_path):
    value = _parsed(capsys)
    plan = copy.deepcopy(value)
    # Only placeholder content is replaced: image base64 strings become real encoded bytes.
    for name, content in plan.get("reference_files", {}).items():
        if isinstance(content, dict) and isinstance(content.get("base64"), str):
            content["base64"] = base64.b64encode(PNG).decode("ascii")
    try:
        files = order.validate_plan(plan, tmp_path, Config())
    except order.PlanError as exc:
        raise AssertionError(
            f"The help example must use field names and value types the plan validator accepts: {exc}"
        ) from None
    for name in plan.get("reference_files", {}):
        assert name in files, f"Illustrated reference file {name} is published by the plan"
    for item in plan["issues"]:
        for name in item.get("acceptance_files", {}):
            assert name in files, f"Illustrated acceptance file {name} is published by the plan"
