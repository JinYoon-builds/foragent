import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent / "acceptance"))
from test_wheel_scroll import run_session  # noqa
from test_skills_autosync import isolated_home  # noqa

def test_probe(tmp_path, monkeypatch, isolated_home):
    s = run_session(tmp_path, monkeypatch, 80, "terminal")
    pathlib.Path(".foragent/probe.txt").write_text(s.frames["baseline-close"])
