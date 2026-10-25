"""Top-level `fa` command groups, one module per group, found by discovery.

Each module defines ``ORDER`` (its place in ``fa --help``), ``add(commands)``
that adds its own parser to the top-level subparsers, and ``run(args)`` that
handles a parsed command and returns the exit code. The module's file name is
the command name. Handlers read their dependencies from ``foragent.cli`` when
they run, so names patched there keep reaching every command.
"""

import importlib
from pathlib import Path
import pkgutil
import sys
from types import ModuleType


def discover() -> list[ModuleType]:
    """Every group module on this package's current __path__, in help order."""
    found = [importlib.import_module(info.name)
             for info in pkgutil.iter_modules(__path__, __name__ + ".") if not info.ispkg]
    return sorted(found, key=lambda module: (module.ORDER, module.__name__))


def module(name: str) -> ModuleType:
    return importlib.import_module(f"{__name__}.{name}")


def facade() -> ModuleType:
    from foragent import cli
    return cli


def repo_store(root: Path):
    """Config, GitHub slug and state store, the setup most local commands share."""
    cli = facade()
    config = cli.load_config(root)
    repo = config.repo or cli.GitRepo(root).remote_slug()
    return config, repo, cli.StateStore.for_repo(repo)


def _open_lane_view(root: Path) -> None:
    """Load the completed setup without making an optional view fail the command."""
    cli = facade()
    if cli.os.environ.get("FORAGENT_VIEW") == "off":
        return
    language = "en"
    try:
        config = cli.load_config(root)
        language = config.language
        repo = config.repo or cli.GitRepo(root).remote_slug()
        cli.watch_open.open_view(root, cli.StateStore.for_repo(repo), language)
    except (cli.ConfigError, cli.GitError, OSError) as exc:
        error = " ".join(str(exc).split())[:200]
        print("foragent: " + cli.text("watch_open_failed", language, error=error), file=sys.stderr)
