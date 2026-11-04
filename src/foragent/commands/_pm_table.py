"""PM task context from the currently registered CLI parser tree."""


def render() -> str:
    """Build in process, following registered paths rather than copying command names."""
    import argparse
    import shlex

    from foragent import cli

    rows = []

    def visit(parser, path):
        if task := parser._defaults.get("pm_task"):
            rows.append(f"- {task}: `{shlex.join(path)}`")
        for action in parser._actions:
            if task := getattr(action, "pm_task", None):
                rows.append(f"- {task}: `{shlex.join([*path, action.option_strings[0]])}`")
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    visit(child, [*path, name])

    visit(cli._parser(), ["fa"])
    return "\n".join([*rows, "Use these fa commands for PM tasks."])
