"""Done-when commands run once with host permissions in a throwaway copy, for the reviewer (#2129).

The isolated reviewer cannot run a project's real test command: its sandbox has no network and
no writable place outside its tree, so tools such as uv (a user cache) or `fa test` (the machine's
test-slot lock) stop before pytest collects anything. Prepare therefore runs each written Done when
command itself, as `fa order red` and the lanes already do on this host: in a new copy of the
candidates' base with exactly the reviewer's files and candidates, never in the caller's checkout.
The reviewer gets the measured command, exit code and output; its own seat stays isolated.

Each copy is checked like the reviewer's tree before it is removed; a run that changed the copy
voids the measurement. The copy lives at ``review-N/check`` and is removed afterwards; the combined output is kept in
``review-N/check.log`` for a person to read later.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from foragent import capacity, order_scratch, processes

OUTPUT_TAIL = 20_000  # the end holds pytest's summary and the runner's last line


def run(git, base: str, context, place, verify, commands: list[str], directory: Path,
        timeout_s: int) -> list[dict]:
    """``{"command", "exit_code", "timed_out", "output", "changed"}`` per command.

    Each command gets its own new copy at ``directory/check``, so nothing one command leaves behind
    reaches the next. ``place(tree)`` adds the candidates exactly as the reviewer's tree has them;
    ``verify(tree, manifest, before)`` is the reviewer tree's own integrity check, run on the copy
    after the command: a run that changed, removed or touched a candidate or any other file of base
    measured something else, so its ``changed`` paths void it. The prepare caller already holds a
    test slot, so ``capacity.HELD_ENV`` keeps an inner `fa test` from waiting for a second one.
    """
    copy, log = directory / "check", directory / "check.log"
    results = []
    for command in commands:
        try:
            manifest = order_scratch.build(git, base, copy, context)
            place(copy)
            before = order_scratch.stamps(copy, order_scratch._entries(copy))  # candidates too
            with log.open("ab") as handle:
                handle.write(f"$ {command}\n".encode("utf-8"))
                start = handle.tell()
            code, timed_out, _ = processes.run_logged(
                ["/bin/sh", "-c", command], cwd=copy, timeout_s=timeout_s, log_path=log,
                env=dict(capacity.HELD_ENV))
            changed = verify(copy, manifest, before)
        finally:
            shutil.rmtree(copy, ignore_errors=True)
        with log.open("rb") as handle:
            handle.seek(start)
            output = handle.read().decode("utf-8", "replace")
        results.append({"command": command, "exit_code": code, "timed_out": timed_out,
                        "output": output[-OUTPUT_TAIL:], "changed": changed})
    return results


def changed(results: list[dict]) -> list[str]:
    """Every path a host run changed in its copy; any of them voids the measurement."""
    return sorted({name for run_ in results for name in run_["changed"]})


def section(results: list[dict]) -> str:
    """The prompt section that hands the measured results to the reviewer."""
    if not results:
        return "(no command was run)\n"
    parts = []
    for run_ in results:
        status = "timed out" if run_["timed_out"] else f"exit code {run_['exit_code']}"
        parts.append(f"### `{run_['command']}` ({status})\n```\n{run_['output']}\n```\n")
    return "".join(parts)


def contradicted(red: list[dict], results: list[dict]) -> list[str]:
    """Commands whose reported exit code differs from the one prepare measured."""
    measured = {run_["command"]: run_["exit_code"] for run_ in results if not run_["timed_out"]}
    return [run_.get("command") for run_ in red
            if run_.get("command") in measured and run_.get("exit_code") != measured[run_["command"]]]
