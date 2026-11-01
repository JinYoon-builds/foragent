"""Whether the acceptance files an order publishes collect in the tree main would receive.

This is a factual importability check. A clean copy of the base plus exactly this order's
acceptance files is collected, never executed, with the project's own test command. Caller
files, caller import paths and other orders' files are not part of that tree. A check
that cannot establish a result refuses too, and says so apart from an import error.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from types import SimpleNamespace
import xml.etree.ElementTree as ET

from foragent import capacity, order_scratch, processes
from foragent.config import Config
from foragent.git import GitError, GitRepo
from foragent.i18n import text
from foragent.issue import acceptance_paths

# Slot admission and execution share this budget, per order, however many bases it is checked on.
LIMIT_S = 30.0
_OUTPUT_CHARS = 1500
# Inherited settings that add import paths or choose another project; the command may set its own.
_CALLER_SETTINGS = "unset PYTHONPATH PYTHONUSERBASE PYTEST_PLUGINS UV_PROJECT UV_WORKING_DIRECTORY; "
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*=.*", re.S)


@dataclass(frozen=True)
class Collected:
    """The base commit this order last collected on and the budget used so far."""
    commit: str
    spent: float


def acceptance_files(plan: dict, files: dict[str, str | bytes], config: Config) -> dict[str, str | bytes]:
    """The published contents of this order's acceptance files: explicit and from Done when."""
    names: set[str] = set()
    for item in [*plan["issues"], *plan.get("edits", [])]:
        names.update(item.get("acceptance_files", {}))
        names.update(acceptance_paths(item["done_when"], config.acceptance_dir))
    # A statement issue's tests may not be written yet; they are not in ``files`` then.
    return {name: files[name] for name in sorted(names) if name in files}


def _executed_file(segment: list[str]) -> str | None:
    """The file one shell segment runs as a program: ``x.py``, ``python x.py``, ``uv run python x.py``."""
    words = list(segment)
    while words and _ASSIGNMENT.fullmatch(words[0]):
        words.pop(0)
    if words[:2] == ["uv", "run"]:
        words = words[2:]
    if words and re.fullmatch(r"python[0-9.]*", Path(words[0]).name):
        words = [word for word in words[1:] if not word.startswith("-")]
    return words[0] if words else None


def _scripts(plan: dict, config: Config) -> set[str]:
    """Files a Done when runs as a program and never hands to pytest: importing one would execute it.

    Only that exact shape counts. Any other mention (``cat x.py``, an unknown wrapper) leaves
    the file collected, so an unrecognized command can only check more, never less.
    """
    handed, run = set(), set()
    for item in [*plan["issues"], *plan.get("edits", [])]:
        for command in item["done_when"]:
            try:
                lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;()")
                lexer.whitespace_split = True
                lexer.commenters = ""
                words = list(lexer)
            except ValueError:
                continue
            segment: list[str] = []
            for word in [*words, ";"]:
                if word and all(char in "|&;()" for char in word):
                    if any(Path(part).name == "pytest" for part in segment):
                        handed.update(acceptance_paths([shlex.join(segment)], config.acceptance_dir))
                    elif (executed := _executed_file(segment)) is not None:
                        run.update(acceptance_paths([executed], config.acceptance_dir))
                    segment = []
                else:
                    segment.append(word)
    return run - handed


def _targets(plan: dict, selected: dict[str, str | bytes], config: Config) -> list[str]:
    """Every Python acceptance file this order publishes, except scripts a Done when runs itself."""
    scripts = _scripts(plan, config)
    return [name for name in selected if name.endswith(".py") and name not in scripts]


def _leaves_copy(value: str) -> bool:
    """Whether a path in this word could resolve outside the directory the command runs in."""
    for piece in re.split(r"[=:,]", value):
        if piece.startswith(("/", "~")) or ".." in PurePosixPath(piece).parts:
            return True
    return False


def _outside(pytest: str, root: Path) -> str | None:
    """The first word of the reduced command that can reach beyond the clean copy, if any.

    The launcher may live anywhere on the machine except the caller's checkout. Every other
    word must stay relative: those paths resolve in the copy, so they only see what main has.
    """
    words = shlex.split(pytest)
    launcher = next(index for index, word in enumerate(words) if not _ASSIGNMENT.fullmatch(word))
    program = words[launcher]
    if ".." in PurePosixPath(program).parts or (program.startswith("/") and Path(
            os.path.normpath(program)).parent.resolve().is_relative_to(root.resolve())):
        return program
    for word in words[:launcher] + words[launcher + 1:]:
        # A short option can carry its value in the same word: -c/etc/pytest.ini.
        if _leaves_copy(word) or (re.match(r"-[A-Za-z]", word) and _leaves_copy(word[2:])):
            return word
    return None


def _search_path(tree: Path, root: Path) -> dict[str, str]:
    """The child's PATH without the caller's checkout: a bare ``pytest`` must not start its environment."""
    path = processes._child_environment(tree).get("PATH")
    if path is None:
        return {}
    caller = root.resolve()
    return {"PATH": os.pathsep.join(entry for entry in path.split(os.pathsep) if not (
        entry.startswith("/") and Path(entry).resolve().is_relative_to(caller)))}


def _has_origin(git: GitRepo) -> bool:
    try:
        git._call(["remote", "get-url", "origin"])
    except GitError:
        return False
    return True


def _current_base(git: GitRepo, branch: str) -> str:
    """The commit this order would land on, before the publication lock refreshes the checkout.

    With origin configured a failed refresh is an error: a stale branch certifies nothing.
    Local commits the remote lacks stay, because publication pushes them along.
    """
    if not _has_origin(git):
        return git.ref_sha(f"refs/heads/{branch}")
    git.fetch("origin", branch)
    remote = git.ref_sha(f"refs/remotes/origin/{branch}")
    try:
        local = git.ref_sha(f"refs/heads/{branch}")
    except GitError:
        return remote
    return local if git.is_ancestor(remote, local) else remote


def _write(tree: Path, name: str, content: str | bytes) -> None:
    # The base may hold a link at or above this path; the overlay never writes through it.
    order_scratch._own_parents(tree, name, create=True)
    path = tree / name
    order_scratch._remove_path(path)
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)


def _errors(report: Path, tree: Path) -> list[tuple[str, str]] | None:
    """(path, error text) of each collection error in the report; None without a usable report."""
    try:
        cases = list(ET.parse(report).iter("testcase"))
    except (OSError, ET.ParseError):
        return None
    found = []
    for case in cases:
        if (error := case.find("error")) is not None:
            detail = (error.text or error.get("message") or "").replace(f"{tree}/", "").strip()
            found.append((case.get("file") or case.get("name") or "?", detail[-_OUTPUT_CHARS:]))
    return found


def _output(log: Path, tree: Path) -> str:
    try:
        return log.read_text(encoding="utf-8", errors="replace").replace(f"{tree}/", "").strip()[-_OUTPUT_CHARS:]
    except OSError:
        return ""


class _Unavailable(Exception):
    """The check established no result; the first argument is the cause, the second the command."""


def _collect(git: GitRepo, base: str, selected: dict[str, str | bytes], targets: list[str], work: Path,
             root: Path, config: Config, language: str, deadline: float) -> list[tuple[str, str]]:
    from foragent.order import detect_test_commands
    from foragent.run_checks import _pytest_command

    tree, report, log = work / "tree", work / "collect.xml", work / "collect.log"
    order_scratch.build(git, base, tree)
    for name, content in selected.items():
        _write(tree, name, content)
    # The clean copy decides, so caller-only project files cannot select a command.
    command = config.test_command or next(iter(detect_test_commands(tree)), None)
    if command is None:
        raise _Unavailable(text("order_collect_no_command", language), None)
    pytest = _pytest_command(command)
    if pytest is None:
        raise _Unavailable(text("order_collect_unsupported", language), command)
    attempted = f"{pytest} --collect-only {shlex.join(targets)}"
    if (word := _outside(pytest, root)) is not None:
        raise _Unavailable(text("order_collect_outside", language, word=word), attempted)
    if not targets:
        return []
    invocation = (f"{_CALLER_SETTINGS}{attempted} -o junit_family=legacy --junit-prefix= "
                  f"--junitxml={shlex.quote(str(report))}")
    try:
        with capacity.hold_test_slot(config.test_slots, deadline=deadline,
                                     timed_out=lambda: text("order_collect_slot_timeout", language)):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(text("order_collect_timeout", language, seconds=int(LIMIT_S)))
            code, timed_out, _ = processes.run_logged(
                ["/bin/sh", "-c", invocation], cwd=tree, timeout_s=remaining, log_path=log,
                env={**capacity.HELD_ENV, "PYTHONDONTWRITEBYTECODE": "1", **_search_path(tree, root)})
    except (TimeoutError, OSError) as exc:
        raise _Unavailable(str(exc), attempted) from exc
    if timed_out:
        raise _Unavailable(text("order_collect_timeout", language, seconds=int(LIMIT_S)), attempted)
    errors = _errors(report, tree)
    if errors:
        return errors
    # 5 is a successful collection without test functions, as for a helper module.
    if errors is None or code not in {0, 5}:
        raise _Unavailable(text("order_collect_exit", language, code=code, output=_output(log, tree)), attempted)
    return []


def check(plan: dict, files: dict[str, str | bytes], root: Path, config: Config, git: GitRepo | None = None, *,
          refreshed: bool = False, earlier: Collected | None = None) -> Collected | None:
    """Refuse unless this order's acceptance files collect on its base; None when it has none.

    ``refreshed`` means the caller holds the publication lock after refreshing the checkout,
    so HEAD is the commit written on. ``earlier`` is this order's previous result: the same
    base is not collected twice and a new base gets only what remains of the budget.
    """
    from foragent.order import PlanError

    selected = acceptance_files(plan, files, config)
    if not selected:
        return None
    language = (getattr(config, "language", None) or "en").lower().split("-")[0]

    def unavailable(cause: str, command: str | None) -> PlanError:
        return PlanError(text("order_collect_unavailable", language, files=", ".join(selected),
                              command=command or text("order_collect_no_command_name", language), cause=cause))

    git = git or GitRepo(root)
    try:
        base = git.head_sha() if refreshed else _current_base(git, config.base_branch)
    except (GitError, OSError, subprocess.SubprocessError) as exc:
        cause = text("order_collect_no_base", language, branch=config.base_branch, error=" ".join(str(exc).split()))
        raise unavailable(cause, config.test_command) from exc
    if earlier is not None and earlier.commit == base:
        return earlier
    spent = earlier.spent if earlier is not None else 0.0
    started = time.monotonic()
    work = None
    try:
        targets = _targets(plan, selected, config)
        work = Path(tempfile.mkdtemp(prefix="foragent-collect-"))
        errors = _collect(git, base, selected, targets, work, root, config, language, started + LIMIT_S - spent)
    except _Unavailable as exc:
        raise unavailable(exc.args[0], exc.args[1]) from exc
    except Exception as exc:  # Whatever stopped the check, it did not establish a pass.
        raise unavailable(f"{type(exc).__name__}: {' '.join(str(exc).split())}", config.test_command) from exc
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
    if errors:
        raise PlanError(text("order_collect_refused", language, base=base[:12], files=", ".join(targets),
                             errors="\n".join(f"{path}:\n{detail}" for path, detail in errors)))
    return Collected(base, spent + time.monotonic() - started)


def land(git: GitRepo, branch: str, checked: list[Collected | None], recheck) -> None:
    """Fast-forward the checkout onto the base just collected on; a refusal leaves it where it was.

    Normally that is the commit already checked. A remote move during this refresh calls
    ``recheck`` on the new HEAD, and if that does not pass the fast-forward is undone first.
    """
    from foragent.order_flush_recovery import _install

    before = git.head_sha()
    git.update_base(branch)
    head = git.head_sha()
    if all(result is None or result.commit == head for result in checked):
        return
    try:
        recheck()
    except BaseException:  # Whatever stopped the check, nothing vouches for the new HEAD.
        if head != before:
            _install(git, SimpleNamespace(base_branch=branch), head, before)
        raise
