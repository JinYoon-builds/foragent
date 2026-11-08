"""n84 regression controls (pass on main; kept outside Done when).

Help output already hides `_sweep`, and the internal hook must stay callable.
"""

import io
import json

from foragent import cli

PUBLIC = ("add", "stop-check", "drop")


def _run(argv, capsys, monkeypatch, stdin=""):
    monkeypatch.delenv("FORAGENT_ROLE", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    try:
        code = cli.main(argv)
    except SystemExit as stop:
        code = stop.code
    out, err = capsys.readouterr()
    return code, out, err


def test_help_stays_free_of_internal_action(capsys, monkeypatch, tmp_path):
    code, out, err = _run(["decisions", "--help"], capsys, monkeypatch)
    assert code == 0
    assert "_sweep" not in out + err
    for action in PUBLIC:
        assert action in out


def test_internal_sweep_hook_still_runs(capsys, monkeypatch, tmp_path):
    payload = json.dumps({"hook_event_name": "NotADecisionEvent"})
    code, out, err = _run(["decisions", "_sweep", "--repo-root", str(tmp_path)], capsys, monkeypatch,
                          stdin=payload)
    assert code == 0, out + err
    assert "invalid choice" not in err
    assert not (tmp_path / ".foragent").exists()
