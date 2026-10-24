"""Acceptance test for upstream-reports. Locked by Foragent; do not edit."""

from datetime import timedelta
import json
import os
from pathlib import Path
import re
import socket
import subprocess

import pytest
import yaml

from test_local_truth import record_gh
from test_orch_review_handoff import bind_cli, make_case
from test_skills_autosync import cli_exit, isolated_home, write_global_config


# Chosen public names: global upstream_reports: bool; --from flag:N or event:N
# (one-based journal line); propose --json returns id, title, body, signature.
# Only title/body are sent. The signature identifies error type + package frame.
UPSTREAM = "JinYoon-builds/foragent"
PRIVATE_REPO = "PRIVATE_OWNER_7c91/PRIVATE_REPOSITORY_7c91"
TITLE = "PRIVATE_ISSUE_TITLE_7c91"
BODY = "PRIVATE_ISSUE_BODY_7c91"
CODE = 'return "PRIVATE_USER_CODE_7c91"'
CONFIG_VALUE = "PRIVATE_CONFIG_VALUE_7c91"
TOKEN = "sk-PRIVATE_CREDENTIAL_7c91"
USER_PATH = "/private/PRIVATE_HOME_7c91/customer_private_7c91.py"
LOOKALIKE = "/private/PRIVATE_HOME_7c91/foragent/customer_lookalike_7c91.py"
STDLIB = "/opt/PRIVATE_PYTHON_7c91/threading_private_7c91.py"


@pytest.fixture(autouse=True)
def offline_only(monkeypatch, isolated_home):
    from foragent import executors

    def forbidden(*args, **kwargs):
        pytest.fail("Upstream reports must use offline fixtures, without network, signals or model turns")

    for executor in (executors.CodexExecutor, executors.ClaudeExecutor):
        monkeypatch.setattr(executor, "run", forbidden)
    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(isolated_home / ".claude"))
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_home / ".local/state"))
    popen = subprocess.Popen

    def local_git_only(argv, *args, **kwargs):
        assert isinstance(argv, (list, tuple)) and argv[0] == "git", (
            f"Unrecorded external command: {argv!r}")
        assert not {"fetch", "push", "pull", "ls-remote", "submodule"}.intersection(argv), argv
        if "clone" in argv:
            assert Path(argv[argv.index("clone") + 1]).is_dir(), "Only fixture-local clones are allowed"
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", local_git_only)


def option(argv, *names):
    for index, arg in enumerate(argv):
        for name in names:
            if arg == name:
                return argv[index + 1]
            if arg.startswith(name + "="):
                return arg[len(name) + 1:]
    return None


class RecordedUpstream:
    """Use record_gh's subprocess seam, including stdin and body-file payloads.

    Accept gh issue/search commands and the equivalent REST API transport used
    by GhTracker. No real gh process can run, even for an unexpected command.
    """

    def __init__(self, monkeypatch):
        self.matches, self.searches, self.writes = [], [], []
        self.payload = {}
        self.calls = record_gh(monkeypatch, reply=self.reply)
        recorded_run = subprocess.run

        def run(argv, *args, **kwargs):
            if isinstance(argv, (list, tuple)) and argv and argv[0] == "gh":
                raw = kwargs.get("input")
                input_file = option(argv, "--input")
                if input_file and input_file != "-":
                    raw = Path(input_file).read_text(encoding="utf-8")
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                try:
                    self.payload = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    self.payload = {"body": raw}
                for index, arg in enumerate(argv):
                    if arg in {"-f", "--raw-field", "-F", "--field"}:
                        key, _, value = argv[index + 1].partition("=")
                        self.payload[key] = value
                body_file = option(argv, "--body-file", "-F") if argv[1:2] != ["api"] else None
                if body_file:
                    self.payload["body"] = (raw if body_file == "-" else
                                            Path(body_file).read_text(encoding="utf-8"))
            return recorded_run(argv, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", run)

    def reply(self, argv):
        if argv == ["auth", "status"]:
            return ""
        endpoint = next((arg for arg in argv if arg.startswith(("repos/", "search/issues"))), "")
        is_api = argv[:1] == ["api"]
        if argv[:2] in (["issue", "list"], ["search", "issues"]) or endpoint.startswith("search/issues"):
            query = option(argv, "--search", "-S") or self.payload.get("q", "")
            if argv[:2] == ["search", "issues"]:
                query = argv[2]
            self.searches.append({"argv": list(argv), "query": query})
            matches = self.matches
            if option(argv, "--state") == "open" or "is:open" in query or "state:open" in query:
                matches = [row for row in matches if row["state"].lower() == "open"]
            if is_api:
                matches = [{**row, "state": row["state"].lower(), "labels": [],
                            "html_url": row["url"]} for row in matches]
                return json.dumps({"items": matches, "total_count": len(matches)})
            return json.dumps(matches)
        action = argv[1] if argv[:1] == ["issue"] and len(argv) > 1 else None
        number = argv[2] if action == "comment" else None
        if is_api and endpoint.startswith(f"repos/{UPSTREAM}/issues"):
            assert option(argv, "-X", "--method") in {None, "POST"}, argv
            action = "comment" if endpoint.endswith("/comments") else "create"
            number = endpoint.split("/")[-2] if action == "comment" else None
        assert action in {"create", "comment"}, f"Unexpected gh request: {argv!r}"
        repository = UPSTREAM if is_api else option(argv, "--repo", "-R")
        assert repository == UPSTREAM, "Reports must go to Foragent upstream, never the user's repository"
        body = option(argv, "--body", "-b") or self.payload.get("body")
        title = option(argv, "--title", "-t") or self.payload.get("title")
        assert isinstance(body, str) and body.strip(), "An upstream write needs its complete reviewed body"
        self.writes.append({"action": action, "number": number, "title": title, "body": body,
                            "argv": list(argv)})
        url = f"https://github.com/{UPSTREAM}/issues/{number or 901}"
        return json.dumps({"id": 901, "number": 901, "html_url": url}) if is_api else url + "\n"


@pytest.fixture
def report_case(git_history, tmp_path, monkeypatch):
    from foragent import cli
    from foragent.config import load_config
    from foragent.tracker import GhTracker

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], language="en")
    case.main = bind_cli(case, monkeypatch)
    # Exercise persisted config and real GhTracker transport instead of letting
    # bind_cli's in-memory config/tracker hide consent or outbound writes.
    monkeypatch.setattr(cli, "load_config", load_config)
    monkeypatch.setattr(cli, "GhTracker", GhTracker)
    case.global_path = Path.home() / ".config/foragent/config.yml"
    case.files_root = tmp_path
    case.gh = RecordedUpstream(monkeypatch)
    return case


def consent(case, enabled):
    values = yaml.safe_load(case.global_path.read_text(encoding="utf-8"))
    if enabled is None:
        values.pop("upstream_reports", None)
    else:
        values["upstream_reports"] = enabled
    write_global_config(values)


def seed_failure(case, *, error_type="RuntimeError", frame="runner.py", function="run_issue", suffix=""):
    import foragent
    from foragent.orch_events import append_event

    package = Path(foragent.__file__).resolve().parent
    trace = (
        'Traceback (most recent call last):\n'
        f'  File "{STDLIB}", line 701, in private_thread_7c91\n'
        f'  File "{USER_PATH}", line 702, in private_customer_7c91\n    {CODE}\n'
        f'  File "{LOOKALIKE}", line 703, in private_lookalike_7c91\n'
        f'  File "{package / frame}", line 704, in {function}\n'
        f'    raise {error_type}("{TOKEN}{suffix}")\n'
        f'{error_type}: {TOKEN}{suffix} {CONFIG_VALUE} {PRIVATE_REPO} {TITLE} {BODY}\n'
    )
    (case.root / ".foragent.yml").write_text(yaml.safe_dump({
        "repo": PRIVATE_REPO, "skill_libraries": [], "language": "en",
        "test_command": f"echo {CONFIG_VALUE}", "notify": f"https://example.invalid/{TOKEN}",
        "profile": {"kind": CONFIG_VALUE}, "stall_minutes": 4317,
    }), encoding="utf-8")
    case.store.update_local_issue(1, title=TITLE, body=BODY + "\n" + CODE, labels=["blocked"], state="open")
    case.store.update_run(1, attempts=3, waited_seconds=12.5, detail=trace, session_body=BODY + CODE,
                          worktree=USER_PATH, branch=PRIVATE_REPO)
    case.store.save_flag(1, trace)
    log = case.store.log_path(1)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(trace + BODY + CODE, encoding="utf-8")
    case.clock[0] += timedelta(seconds=37.5)
    event = append_event(case.store, case.clock[0], 1, "error", trace)
    return event


def propose(case, capsys, source="flag:1"):
    capsys.readouterr()
    code = cli_exit(case.main, ["report", "propose", "--from", source, "--json"])
    output = capsys.readouterr()
    assert code == 0, f"fa report propose must prepare a local, reviewable report: {output.err.strip()}"
    report = json.loads(output.out)
    assert {"id", "title", "body", "signature"} <= report.keys(), "Show the entire report and its send ID"
    assert all(isinstance(report[key], str) and report[key].strip()
               for key in ("id", "title", "body", "signature"))
    assert case.gh.writes == [], "Proposing a report is never permission to send it"
    return report


def send(case, report, *approval):
    return cli_exit(case.main, ["report", "send", report["id"], *approval])


def assert_private_data_absent(text, case):
    import foragent

    # Check both full values and components: basename-only and owner-only leaks
    # still violate the contract, as do user frames merely containing /foragent/.
    forbidden = [TITLE, BODY, CODE, "PRIVATE_USER_CODE_7c91", CONFIG_VALUE, TOKEN, "4317",
                 *PRIVATE_REPO.split("/"), USER_PATH, LOOKALIKE, STDLIB,
                 "customer_private_7c91.py", "customer_lookalike_7c91.py", "threading_private_7c91.py",
                 "private_thread_7c91", "private_customer_7c91", "private_lookalike_7c91",
                 "PRIVATE_HOME_7c91", "PRIVATE_PYTHON_7c91", str(case.root), str(Path.home()),
                 str(Path(foragent.__file__).resolve().parent.parent)]
    for value in forbidden:
        assert value not in text, f"Upstream report leaked private input: {value!r}"


@pytest.mark.parametrize("answer,enabled", [("", False), ("n", False), ("y", True)],
                         ids=["default-no", "declined", "accepted"])
def test_upstream_reports_init_asks_once_and_remembers_user_consent(
    report_case, monkeypatch, capsys, answer, enabled,
):
    from foragent import cli
    from foragent.init import init_repo

    case = report_case
    monkeypatch.setattr(case.git, "remote_slug", lambda: "owner/repo")
    # Setup Git mutations are irrelevant to consent; record them, never commit.
    setup_writes = []
    monkeypatch.setattr(case.git, "commit_paths", lambda *args: setup_writes.append(args))
    monkeypatch.setattr(cli, "init_repo", lambda root, **kwargs: init_repo(
        root, git=case.git, tracker=case.tracker, **kwargs))
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or answer)
    argv = ["init", "--no-push", "--no-launchd", "--no-board", "--statusline", "no"]
    assert cli_exit(case.main, argv) == 0
    assert len(prompts) == 1, "fa init must ask once whether upstream reports may be proposed (default no)"
    assert "upstream" in prompts[0].lower() and "report" in prompts[0].lower()
    saved = yaml.safe_load(case.global_path.read_text(encoding="utf-8"))
    assert saved.get("upstream_reports") is enabled, "Persist both acceptance and refusal in the user's config"
    assert saved["skill_libraries"] == [], "Remember consent without replacing unrelated user settings"
    assert "upstream_reports" not in yaml.safe_load((case.root / ".foragent.yml").read_text())

    def no_second_question(prompt=""):
        pytest.fail("fa init must remember the user's upstream-report decision, including a refusal")

    monkeypatch.setattr("builtins.input", no_second_question)
    assert cli_exit(case.main, argv) == 0
    assert yaml.safe_load(case.global_path.read_text()) == saved
    assert case.gh.writes == []


@pytest.mark.parametrize("enabled", [None, False], ids=["unset", "declined"])
def test_upstream_reports_without_opt_in_cannot_propose_or_send(report_case, capsys, enabled):
    case = report_case
    seed_failure(case)
    consent(case, enabled)
    # A missing command is not evidence that the consent boundary works.
    code = cli_exit(case.main, ["report", "--help"])
    output = capsys.readouterr()
    assert code == 0, f"The report CLI must exist before testing opt-in refusal: {output.err.strip()}"
    before = {p: p.read_bytes() for p in case.files_root.rglob("*") if p.is_file()}
    assert cli_exit(case.main, ["report", "propose", "--from", "flag:1", "--json"]) != 0
    assert cli_exit(case.main, ["report", "send", "unapproved-report", "--yes"]) != 0
    assert {p: p.read_bytes() for p in case.files_root.rglob("*") if p.is_file()} == before
    assert case.gh.calls == [], "Without opt-in there must be no upstream search or write"


@pytest.mark.parametrize("source", ["flag:1", "event:1"], ids=["flag", "event"])
def test_upstream_reports_proposal_shows_full_content_without_sending(report_case, capsys, source):
    case = report_case
    seed_failure(case)
    consent(case, True)
    report = propose(case, capsys, source)
    assert "RuntimeError" in report["body"] and "foragent/runner.py" in report["body"]
    assert send(case, report, "--yes") == 0
    assert len(case.gh.writes) == 1
    sent = case.gh.writes[0]
    assert sent["body"] == report["body"], "Send exactly the full body shown for approval"
    if sent["action"] == "create":
        assert sent["title"] == report["title"], "The approved title is part of the report too"


def test_upstream_reports_each_report_needs_explicit_send_approval(report_case, monkeypatch, capsys):
    case = report_case
    seed_failure(case)
    consent(case, True)
    first = propose(case, capsys)
    # A different local failure must not inherit the first report's approval.
    case.store.save_flag(2, case.store.load_flag(1).reason.replace("RuntimeError", "ValueError"))
    second = propose(case, capsys, "flag:2")
    assert first["id"] != second["id"]
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert send(case, first) != 0, "No --yes and an empty answer must decline sending"
    assert case.gh.writes == []
    assert send(case, first, "--yes") == 0
    assert len(case.gh.writes) == 1
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert send(case, second) != 0, "Consent to one report does not approve another report"
    assert len(case.gh.writes) == 1


@pytest.mark.parametrize("enabled", [None, False], ids=["removed", "revoked"])
def test_upstream_reports_send_rechecks_current_user_opt_in(report_case, capsys, enabled):
    case = report_case
    seed_failure(case)
    consent(case, True)
    report = propose(case, capsys)
    consent(case, enabled)
    calls = len(case.gh.calls)
    assert send(case, report, "--yes") != 0, "--yes must not override missing or revoked user opt-in"
    assert len(case.gh.calls) == calls, "Revoked consent must be checked before any upstream access"
    assert case.gh.writes == []


@pytest.mark.parametrize("source", ["flag:1", "event:1"], ids=["flag", "event"])
def test_upstream_reports_only_package_facts_survive_private_inputs(report_case, capsys, source):
    import foragent

    case = report_case
    seed_failure(case)
    consent(case, True)
    report = propose(case, capsys, source)
    public = report["title"] + "\n" + report["body"] + "\n" + report["signature"]
    assert_private_data_absent(public, case)
    for fact in (foragent.__version__, "test_command", "notify", "profile", "stall_minutes",
                 "RuntimeError", "foragent/runner.py", "run_issue", "12.5"):
        assert fact in public, f"Keep useful Foragent facts while removing private values: {fact}"
    assert re.search(r"\battempts\b\D{0,20}\b3\b", public, re.I), "Keep Foragent's attempt count"
    assert send(case, report, "--yes") == 0
    assert len(case.gh.writes) == 1
    # A local --body-file filename is transport, not report content. Inspect the
    # search query and actual outbound text, including stdin/file-backed bodies.
    outbound = "\n".join(row["query"] for row in case.gh.searches)
    outbound += "\n".join((row["title"] or "") + "\n" + row["body"] for row in case.gh.writes)
    assert_private_data_absent(outbound, case)
    assert case.gh.writes[0]["body"] == report["body"]


def test_upstream_reports_signature_uses_error_type_and_package_frame_not_user_text(report_case, capsys):
    case = report_case
    consent(case, True)
    signatures = []
    for error_type, frame, function, suffix in (
        ("RuntimeError", "runner.py", "run_issue", "-first-private-message"),
        ("RuntimeError", "runner.py", "run_issue", "-different-private-message"),
        ("ValueError", "runner.py", "run_issue", "-first-private-message"),
        ("RuntimeError", "config.py", "load_config", "-first-private-message"),
    ):
        seed_failure(case, error_type=error_type, frame=frame, function=function, suffix=suffix)
        signatures.append(propose(case, capsys)["signature"])
    assert signatures[0] == signatures[1], "Private error messages must not fragment the upstream signature"
    assert len({signatures[0], signatures[2], signatures[3]}) == 3, "Both error type and Foragent frame identify a failure"
    assert case.gh.writes == []


@pytest.mark.parametrize("existing", ["matching-open", "none", "matching-closed", "unrelated-open"])
def test_upstream_reports_search_then_append_only_to_matching_open_issue(report_case, capsys, existing):
    case = report_case
    seed_failure(case)
    consent(case, True)
    report = propose(case, capsys)
    match = {"number": 811, "state": "OPEN", "title": report["title"],
             "body": report["body"] + "\nSignature: " + report["signature"],
             "url": f"https://github.com/{UPSTREAM}/issues/811"}
    if existing == "matching-closed":
        match["state"] = "CLOSED"
    elif existing == "unrelated-open":
        match.update(title="Other Foragent failure", body="ValueError at foragent/config.py:load_config")
    case.gh.matches = [] if existing == "none" else [match]
    case.gh.searches.clear()
    case.gh.calls.clear()
    assert send(case, report, "--yes") == 0
    assert case.gh.searches, "Search upstream again at send time before creating or appending"
    search = case.gh.searches[0]
    assert report["signature"] in search["query"], "Search by the error-type/package-frame signature"
    assert UPSTREAM in " ".join(search["argv"]) + search["query"], "Search Foragent's repository explicitly"
    assert len(case.gh.writes) == 1, "A report must produce one upstream issue or one comment"
    sent = case.gh.writes[0]
    assert case.gh.calls.index(["gh", *search["argv"]]) < case.gh.calls.index(["gh", *sent["argv"]]), (
        "The search must precede the upstream write")
    expected = "comment" if existing == "matching-open" else "create"
    assert sent["action"] == expected, f"{existing}: expected {expected}, got {sent['action']}"
    if expected == "comment":
        assert sent["number"] == "811", "Append to the matching open issue instead of opening a duplicate"
    else:
        assert sent["title"] == report["title"]
    assert sent["body"] == report["body"]
