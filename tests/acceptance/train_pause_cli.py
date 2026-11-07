"""Shared helper for the pause-past-until acceptance files: run `fa orch pause-trains`
through the public CLI (foragent.cli.main) on a scratch repository with a fixed clock,
and read the pause in force the way `fa gate status` and the merge owner read it."""

from datetime import UTC, datetime

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
REASON = "Let queued order publications land first"


def make_cli(tmp_path, monkeypatch, language="en", now=NOW):
    from foragent import cli
    from foragent.state import StateStore

    root = tmp_path / "repo"
    root.mkdir()
    (root / ".foragent.yml").write_text(f"repo: owner/name\nlanguage: {language}\n", encoding="utf-8")
    store = StateStore(tmp_path / "state")
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo, **kw: store))
    monkeypatch.setenv("FORAGENT_VIEW", "off")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz else now.replace(tzinfo=None)

    monkeypatch.setattr(cli, "datetime", Clock)

    def fa(capsys, *argv):
        capsys.readouterr()
        try:
            code = cli.main([*argv, "--repo-root", str(root)])
        except SystemExit as exc:
            code = exc.code
        captured = capsys.readouterr()
        return (0 if code is None else code), captured.out + captured.err

    return root, store, fa


def pause_in_force(root, store, now=NOW):
    """`fa gate status --json`'s train_pause value (status.collect) for this store."""
    from foragent import status
    from foragent.config import load_config

    return status.collect(store, load_config(root), now).get("train_pause")

