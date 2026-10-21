"""Acceptance test for view-version. Locked by Foragent; do not edit."""


def test_version_prints_installed_build_sha_without_reading_live_git(monkeypatch, capsys):
    import importlib
    import subprocess
    import sys
    from types import ModuleType

    import foragent
    from foragent import cli

    build = ModuleType("foragent._build")

    def build_metadata(name):
        # The spec leaves the metadata constant's name to the implementation.
        if name.startswith("__"):
            raise AttributeError(name)
        return "a1b2c3d"

    build.__getattr__ = build_metadata
    monkeypatch.setitem(sys.modules, "foragent._build", build)
    monkeypatch.setattr(foragent, "_build", build, raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("--version must use build metadata, without subprocesses")

    monkeypatch.setattr(subprocess, "run", forbidden)
    importlib.reload(cli)
    try:
        cli.main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    assert capsys.readouterr().out.strip() == f"foragent {foragent.__version__} (a1b2c3d)"


def test_version_missing_build_metadata_falls_back_to_unknown(monkeypatch, capsys):
    import importlib
    import sys
    import foragent
    from foragent import cli

    monkeypatch.setitem(sys.modules, "foragent._build", None)
    monkeypatch.delattr(foragent, "_build", raising=False)
    importlib.reload(cli)
    try:
        cli.main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    assert capsys.readouterr().out.strip() == f"foragent {foragent.__version__} (unknown)"
