"""Regression control for n91 that already passes on main (kept outside Done when).

Publication's executable preflight refuses a guessed bare-python Done when command on a
host whose PATH offers only uv and git, so the supplied-command test in
test_prepare_writer_test_command.py measures a real difference.
"""

from lane_tests_case import assert_no_publication, lane_case, offline_publication, publish  # noqa: F401
from test_prepare_writer_test_command import FALLBACK, _configure, uv_only_host  # noqa: F401


def test_bare_python_guess_is_refused_by_the_same_preflight(uv_only_host, capsys, monkeypatch):
    """Control: without the supplied command the guessed bare-python command is refused."""
    case, _, _ = uv_only_host
    _configure(case, monkeypatch, "uv run pytest -q")
    case.plan["issues"][0]["done_when"] = [FALLBACK]
    case.save()
    capsys.readouterr()
    code, out, err = publish(case, capsys, "--dry-run")
    assert code != 0 and "python" in out + err, out + err
    assert_no_publication(case)
