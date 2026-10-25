"""`fa statusline`: Claude Code statusline wrapper; never needs a repository."""

from foragent.commands import facade

ORDER = 60


def add(commands) -> None:
    statusline_command = commands.add_parser(
        "statusline", help="Claude Code statusline: record rate limits, show your statusline")
    statusline_command.add_argument("--uninstall", action="store_true",
                                    help="Put back the statusline you had before Foragent's wrapper")


def _uninstall_statusline() -> int:
    cli = facade()
    from foragent import statusline
    try:
        language = cli.load_config(cli.Path.cwd()).language
    except Exception:  # Any folder works: outside a Foragent repository, speak English.
        language = "en"
    code, message = statusline.uninstall(language)
    print(message, file=cli.sys.stderr if code else cli.sys.stdout)
    return code


def run(args) -> int:
    """``args`` is None for the bare refresh call Claude Code makes."""
    if args is not None and args.uninstall:
        return facade()._uninstall_statusline()
    from foragent import statusline
    return statusline.run()
