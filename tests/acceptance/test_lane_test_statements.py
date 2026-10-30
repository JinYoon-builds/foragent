"""Statement input and the prepublication boundary, through fa order."""

import pytest

from lane_tests_case import assert_no_publication, lane_case, offline_publication, publish


def test_statement_draft_validates_without_tests_or_side_effects(lane_case, capsys):
    case = lane_case
    before = case.path.read_bytes()
    code, out, err = publish(case, capsys, "--dry-run")
    assert code == 0, f"Plain behaviour statements must be a valid draft: {out}{err}"
    assert case.path.read_bytes() == before and case.calls == []
    assert_no_publication(case)


@pytest.mark.parametrize("approval", [(), ("--approve",), ("--approve-keys", "notice"),
                                      ("--approve-numbers", "2")])
def test_unprepared_statements_cannot_be_published_even_without_approval(lane_case, capsys, approval):
    case = lane_case
    code, out, err = publish(case, capsys, *approval)
    assert code != 0
    message = (out + err).lower()
    assert "notice" in message and "prepare" in message and "review" in message, message
    assert case.calls == []
    assert_no_publication(case)


@pytest.mark.parametrize("value", [[], [""], ["  "], "deliver once", [7]])
def test_invalid_statements_name_the_field_in_the_cli_error(lane_case, capsys, value):
    case = lane_case
    case.plan["issues"][0]["statements"] = value
    case.save()
    code, out, err = publish(case, capsys, "--dry-run")
    assert code != 0 and "statements" in out + err
    # A valid control keeps an unknown-field error from satisfying this test.
    case.plan["issues"][0]["statements"] = ["A caller observes one receipt."]
    case.save()
    code, out, err = publish(case, capsys, "--dry-run")
    assert code == 0, out + err
    assert_no_publication(case)
