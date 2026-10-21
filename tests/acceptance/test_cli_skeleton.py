"""Acceptance: `fa adopt` and `fa bench` exist and delegate to their own modules."""

import pytest

from foragent import cli


@pytest.mark.parametrize("argv, options", [
    (["adopt", "survey", "--help"], ["--repo-root", "--json"]),
    (["adopt", "baseline", "--help"], ["--repo-root", "--json", "--command", "--timeout"]),
    (["adopt", "ci", "--help"], ["--repo-root", "--json", "--exclude-acceptance"]),
    (["bench", "record", "--help"], ["--arm", "--repo", "--started", "--finished", "--state-dir",
                                     "--tokens", "--interventions"]),
    (["bench", "report", "--help"], ["--json"]),
])
def test_new_commands_parse(argv, options, capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(argv)
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    for option in options:
        assert option in out


def test_adopt_delegates_to_adopt_module(monkeypatch, tmp_path):
    from foragent import adopt

    seen = []
    monkeypatch.setattr(adopt, "run", lambda args: seen.append((args.action, args.json)) or 7)
    assert cli.main(["adopt", "survey", "--repo-root", str(tmp_path), "--json"]) == 7
    assert seen == [("survey", True)]


def test_bench_delegates_to_bench_module(monkeypatch, tmp_path):
    from foragent import bench

    seen = []
    monkeypatch.setattr(bench, "run", lambda args: seen.append(args.action) or 0)
    assert cli.main(["bench", "report", str(tmp_path)]) == 0
    assert seen == ["report"]
