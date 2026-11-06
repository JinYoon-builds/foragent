"""Run the fa CLI in-process and keep its exit code and output (copied from the locked one-entrance test)."""

from types import SimpleNamespace

from foragent import cli


def invoke(capsys, *argv):
    capsys.readouterr()
    try:
        code = cli.main(list(map(str, argv)))
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return SimpleNamespace(code=code, out=captured.out, err=captured.err,
                           text=captured.out + captured.err)
