"""`fa dashboard`: the person's name for `fa gate watch`, with the same flags and behaviour."""

from foragent.commands import facade

ORDER = 112


def add(commands) -> None:
    from foragent.commands.gate import add_watch_flags

    add_watch_flags(commands.add_parser("dashboard", help="Show the live dashboard of lanes and work"))


def run(args) -> int:
    from foragent.commands import gate

    args.action = "watch"
    return gate.run(args)
