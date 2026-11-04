"""Regression coverage for adoption reports and CI source preservation."""

import json
import os
import shlex
import sys
import time
from pathlib import Path

import pytest
import yaml

from foragent import adopt, cli


def workflow(root, script, *, filename="ci.yml"):
    path = root / ".github/workflows" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"# keep this comment\r\njobs:\r\n  test:\r\n    steps:\r\n" + script)
    return path


@pytest.mark.parametrize("line", [
    b"      - run: uv run pytest -q  # keep YAML comment\r\n",
    b"      - run: 'python -m pytest -q' # keep quote\r\n",
    b'      - run: "pytest -k \\"hello\\"" # keep escape\r\n',
    b'      - run: "pytest -k \'#tag\' # shell comment"\r\n',
    b"      - run: pytest -q",
    "      - run: pytest -k 한글\r\n".encode(),
])
def test_ci_preserves_every_other_byte(tmp_path, line):
    path = workflow(tmp_path, line)
    before = path.read_bytes()
    assert adopt.ci(tmp_path)["needs_change"] == [".github/workflows/ci.yml"]
    assert path.read_bytes() == before
    assert adopt.ci(tmp_path, True)["changed"] == [".github/workflows/ci.yml"]
    after = path.read_bytes()
    assert after.count(b" --ignore=tests/acceptance") == 1
    assert after.replace(b" --ignore=tests/acceptance", b"") == before
    assert adopt._ci(tmp_path, "tests/acceptance") == [
        {"file": ".github/workflows/ci.yml", "runs_acceptance": False},
    ]
    assert adopt.ci(tmp_path, True) == {
        "needs_change": [], "changed": [], "manual": [],
        "collect_only": [{"file": ".github/workflows/ci.yml",
                          "add": ("uv run pytest" if b"uv run" in line else
                                  "python -m pytest" if b"python -m" in line else "pytest")
                          + " --collect-only -q"}],
    }
    assert path.read_bytes() == after


def test_ci_checks_each_pytest_command_and_keeps_other_runners(tmp_path):
    path = workflow(tmp_path, (
        b"      - run: pytest --ignore=tests/acceptance\r\n"
        b"      - run: echo pytest\r\n"
        b"      - run: npm test\r\n"
        b"      - run: pnpm test --filter app\r\n"
        b"      - run: go test ./...\r\n"
        b"      - run: pytest -q --ignore=elsewhere\r\n"
        b"      - run: pytest --ignore=tests/acceptance-more\r\n"
        b"      - run: pytest -k 'a;b' && pytest tests/unit\r\n"
        b"      - run: pytest 2>&1 | tee results.log\r\n"
    ))
    before = path.read_bytes()
    report = adopt.ci(tmp_path, True)
    assert report["manual"] == [
        {"file": ".github/workflows/ci.yml", "line": line}
        for line in ("npm test", "pnpm test --filter app", "go test ./...")
    ]
    assert report["needs_change"] == report["changed"] == [".github/workflows/ci.yml"]
    assert path.read_bytes() == before.replace(
        b"pytest -q --ignore=elsewhere", b"pytest -q --ignore=elsewhere --ignore=tests/acceptance",
    ).replace(
        b"pytest --ignore=tests/acceptance-more", b"pytest --ignore=tests/acceptance-more --ignore=tests/acceptance",
    ).replace(
        b"pytest -k 'a;b' && pytest tests/unit",
        b"pytest -k 'a;b' --ignore=tests/acceptance && pytest tests/unit --ignore=tests/acceptance",
    ).replace(
        b"pytest 2>&1 | tee results.log", b"pytest 2>&1 --ignore=tests/acceptance | tee results.log",
    )
    assert adopt.ci(tmp_path, True)["needs_change"] == []


@pytest.mark.parametrize("style", ["|", ">-"])
def test_ci_reports_multiline_pytest_without_editing(tmp_path, style):
    path = workflow(tmp_path, f"      - run: {style}\r\n          pytest -q\r\n".encode())
    before = path.read_bytes()
    report = adopt.ci(tmp_path, True)
    assert report["needs_change"] == [".github/workflows/ci.yml"]
    assert report["changed"] == []
    assert report["manual"][0]["line"].strip() == "pytest -q"
    assert path.read_bytes() == before


def test_ci_empty_and_non_workflows_are_ignored(tmp_path):
    assert adopt.ci(tmp_path, True) == {"needs_change": [], "changed": [], "manual": [], "collect_only": []}
    workflow(tmp_path, b"      - run: pytest\n", filename="ci.txt")
    assert adopt.ci(tmp_path, True) == {"needs_change": [], "changed": [], "manual": [], "collect_only": []}


def test_ci_invalid_yaml_returns_error_before_writing(tmp_path, capsys):
    path = workflow(tmp_path, b"      - run: [\n")
    before = path.read_bytes()
    assert cli.main(["adopt", "ci", "--repo-root", str(tmp_path), "--json"]) == 1
    assert "cannot read workflow" in capsys.readouterr().err
    assert path.read_bytes() == before


@pytest.mark.parametrize("quote", ["", "'", '"'])
def test_ci_custom_path_with_spaces(tmp_path, quote):
    (tmp_path / ".foragent.yml").write_text("acceptance_dir: 'checks/locked tests/'\n")
    path = workflow(tmp_path, f"      - run: {quote}pytest -q{quote}\r\n".encode())
    before = path.read_bytes()
    assert adopt.ci(tmp_path, True)["changed"] == [".github/workflows/ci.yml"]
    assert adopt._ci(tmp_path, "checks/locked tests")[0]["runs_acceptance"] is False
    assert adopt.ci(tmp_path, True)["changed"] == []
    assert path.read_bytes().startswith(before.split(b"pytest")[0])


def test_shared_detection_preserves_quoted_hash_and_resumes_after_comments():
    assert adopt._test_invocations("pytest -k '#tag' --ignore=tests/acceptance # comment\nnpm test") == [
        ["pytest", "-k", "#tag", "--ignore=tests/acceptance"], ["npm", "test"],
    ]


@pytest.mark.parametrize("prefix", [
    "pytest", "uv run pytest", "uv run --no-sync pytest", "python -m pytest",
    "poetry run python3 -m pytest", "'/path with spaces/pytest'",
])
def test_ci_collection_preserves_prefix_and_reports_once(tmp_path, capsys, prefix):
    path = workflow(tmp_path, (
        f"      - run: |\n          {prefix} -q --ignore=tests/acceptance\n"
        "          pytest tests/unit\n          echo --collect-only\n"
    ).encode())
    workflow(tmp_path, b"      - run: npm test\n", filename="node.yml")
    before = path.read_bytes()
    expected = {"file": ".github/workflows/ci.yml", "add": prefix + " --collect-only -q"}
    assert adopt.ci(tmp_path)["collect_only"] == [expected]
    assert cli.main(["adopt", "ci", "--repo-root", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert expected["file"] in output and expected["add"] in output
    assert path.read_bytes() == before


def test_ci_collection_detection_is_per_workflow_and_exclusion_keeps_collection(tmp_path):
    path = workflow(tmp_path, (
        b"      - run: pytest --collect-only -q\n"
        b"      - run: uv run pytest -q\n"
    ))
    workflow(tmp_path, b"      - run: python -m pytest -q\n", filename="other.yaml")
    before = path.read_bytes()
    report = adopt.ci(tmp_path, True)
    assert report["collect_only"] == [
        {"file": ".github/workflows/other.yaml", "add": "python -m pytest --collect-only -q"},
    ]
    assert path.read_bytes() == before.replace(
        b"uv run pytest -q", b"uv run pytest -q --ignore=tests/acceptance",
    )
    assert adopt.ci(tmp_path, True)["changed"] == []


def test_ci_yaml_merges_and_anchors_only_insert_once(tmp_path):
    path = workflow(tmp_path, (
        b"      - &tests\r\n"
        b"        run: &command pytest -q\r\n"
        b"      - <<: *tests\r\n"
        b"      - run: *command\r\n"
    ))
    before = path.read_bytes()
    report = adopt.ci(tmp_path, True)
    assert report["changed"] == [".github/workflows/ci.yml"]
    assert path.read_bytes() == before.replace(b"pytest -q", b"pytest -q --ignore=tests/acceptance")
    assert adopt.ci(tmp_path, True)["changed"] == []


@pytest.mark.parametrize(("files", "expected"), [
    ({"pyproject.toml": "", "go.mod": ""}, "uv run pytest"),
    ({"requirements.txt": "pytest\n"}, "uv run pytest"),
    ({"package.json": '{"scripts":{"test":"vitest"}}'}, "npm test"),
    ({"package.json": '{"scripts":{"test":"vitest"}}', "pnpm-lock.yaml": ""}, "pnpm test"),
    ({"package.json": '{"scripts":{"test":"vitest"},"packageManager":"pnpm@10"}'}, "pnpm test"),
    ({"go.mod": "module example\n"}, "go test ./..."),
    ({"package.json": "broken", "go.mod": ""}, "go test ./..."),
    ({"package.json": "[]"}, None),
    ({"package.json": '{"scripts":{"build":"vite build"}}'}, None),
])
def test_baseline_command_detection(tmp_path, files, expected):
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    assert adopt._detect_test_command(tmp_path) == expected


def test_baseline_override_runs_once_in_root_without_parsing_non_pytest(tmp_path, capsys):
    (tmp_path / "pyproject.toml").touch()
    (tmp_path / "runner.py").write_text(
        "from pathlib import Path\n"
        "with Path('runs').open('a') as f: f.write('once\\n')\n"
        "print('FAILED tests/test_example.py::test_example - error')\n"
        "raise SystemExit(3)\n"
    )
    command = shlex.join([sys.executable, "runner.py"])
    assert cli.main(["adopt", "baseline", "--repo-root", str(tmp_path),
                     "--command", command, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "command": command, "exit_code": 3, "passed": False, "failing": [],
    }
    assert (tmp_path / "runs").read_text() == "once\n"


def test_baseline_pytest_reports_colored_parameterized_ids_and_quotes_ignore(tmp_path):
    (tmp_path / ".foragent.yml").write_text("acceptance_dir: 'checks/locked tests/'\n")
    runner = tmp_path / "pytest"
    runner.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" > args\n"
        "printf '\\033[31mFAILED tests/test_example.py::test_case[a b] - AssertionError\\033[0m\\n'\n"
        "exit 1\n"
    )
    runner.chmod(0o755)
    report = adopt.baseline(tmp_path, shlex.quote(str(runner)))
    assert report["passed"] is False
    assert report["failing"] == ["tests/test_example.py::test_case[a b]"]
    assert (tmp_path / "args").read_text().splitlines() == ["--ignore=checks/locked tests", "-rf"]


def test_baseline_timeout_stops_shell_and_runner(tmp_path, capsys, monkeypatch, fake_process_table):
    import subprocess

    class Process:
        pid = 42000
        returncode = None
        calls = 0

        def __init__(self, *args, **kwargs):
            fake_process_table.add(self.pid, cwd=tmp_path, start_time=10)
            fake_process_table.add(42001, ppid=self.pid, pgid=self.pid, sid=self.pid,
                                   cwd=tmp_path, start_time=11)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            assert not {42000, 42001} & fake_process_table.live

        def communicate(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("offline baseline", 1)
            return b"", None

    monkeypatch.setattr(adopt.subprocess, "Popen", Process)
    assert cli.main(["adopt", "baseline", "--repo-root", str(tmp_path),
                     "--command", "offline-baseline", "--timeout", "1", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["exit_code"] == 124
    assert report["passed"] is False
    assert report["failing"] == []
    assert not {42000, 42001} & fake_process_table.live
    assert all(kind == "pid" and sig != 0 for kind, _, sig in fake_process_table.calls)


def _skill_library(tmp_path, repo, names):
    library = tmp_path / "library"
    for name in names:
        path = library / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: {name}\ndescription: {name} guidance\n---\nBody.\n", encoding="utf-8")
    (library / "LICENSE").write_text("SPDX-License-Identifier: MIT\n", encoding="utf-8")
    config = Path.home() / ".config/foragent/config.yml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(yaml.safe_dump({"skill_libraries": [{"name": "lib", "repo": repo, "path": str(library)}],
                                      "skill_roles": {"lib:voice": "style"}}), encoding="utf-8")


def test_confirmed_proposal_is_recorded_once_and_offered_first(tmp_path, capsys, monkeypatch):
    from foragent.config import load_config
    from foragent.run_skill_catalog import available_skills

    # An unknown source: without a person's confirmation the per-run catalogue offers nothing from it.
    _skill_library(tmp_path, "https://github.com/someone/skills", ["alpha", "beta", "voice"])
    root = tmp_path / "project"
    root.mkdir()
    (root / ".foragent.yml").write_text("# keep me\nlanes:\n  app:\n    paths: [src/**]\n    skills: []\n",
                                        encoding="utf-8")
    monkeypatch.chdir(root)
    assert available_skills(load_config(root), []) == []

    for identity in ("lib:beta", "lib:beta", "lib:voice"):
        assert cli.main(["skills", "prefer", identity, "--repo-root", str(root)]) == 0
    capsys.readouterr()
    text = (root / ".foragent.yml").read_text(encoding="utf-8")
    assert text.startswith("# keep me\nlanes:\n  app:\n    paths: [src/**]\n    skills: []\n")
    config = load_config(root)
    assert config.preferred_skills == ["lib:beta", "lib:voice"]
    assert config.lanes["app"]["skills"] == []  # Preferring never pins a skill on a lane.
    # A preferred style skill stays a person's per-lane pin, so only beta is offered.
    assert [name for name, _, _ in available_skills(config, [])] == ["beta"]

    assert cli.main(["skills", "prefer", "lib:missing", "--repo-root", str(root)]) != 0
    assert load_config(root).preferred_skills == ["lib:beta", "lib:voice"]

    assert cli.main(["skills", "catalog", "--json"]) == 0
    ids = [entry["id"] for entry in json.loads(capsys.readouterr().out) if entry["id"].startswith("lib:")]
    assert ids == ["lib:beta", "lib:voice", "lib:alpha"]


def test_preferred_skills_come_before_other_offered_skills(tmp_path, monkeypatch):
    from foragent.config import load_config
    from foragent.run_skill_catalog import available_skills

    _skill_library(tmp_path, "https://github.com/anthropics/skills", ["alpha", "beta", "gamma"])
    root = tmp_path / "project"
    root.mkdir()
    (root / ".foragent.yml").write_text("preferred_skills: [lib:gamma, lib:beta]\n", encoding="utf-8")
    monkeypatch.chdir(root)
    offered = [name for name, _, _ in available_skills(load_config(root), [])]
    assert [name for name in offered if name in {"alpha", "beta", "gamma"}] == ["gamma", "beta", "alpha"]
    assert offered[:2] == ["gamma", "beta"]


def _signal_project(tmp_path, name, files):
    from conftest import GitHistory, git_command

    origin, clone = tmp_path / f"{name}.git", tmp_path / name
    git_command(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    GitHistory(origin, clone).update({"README.md": f"# {name}\n", **files})
    git_command(tmp_path, "clone", str(origin), str(clone))
    return clone


def test_first_run_fetches_missing_trusted_libraries_for_its_proposals(tmp_path, capsys, monkeypatch):
    from foragent import skills

    # Fresh machine: the default trusted libraries have no local copy yet; an unknown one is never fetched.
    source = tmp_path / "upstream"
    (source / "design/skills/ux-copy").mkdir(parents=True)
    (source / "design/skills/ux-copy/SKILL.md").write_text(
        "---\nname: ux-copy\ndescription: Interface copy\n---\nBody.\n", encoding="utf-8")
    (source / "LICENSE").write_text("SPDX-License-Identifier: MIT\n", encoding="utf-8")
    config = Path.home() / ".config/foragent/config.yml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(yaml.safe_dump({"skill_libraries": [
        {"name": "community", "repo": "https://github.com/unknown-publisher/skills"},
        {"name": "knowledge-work-plugins", "repo": "https://github.com/anthropics/knowledge-work-plugins"},
    ]}), encoding="utf-8")
    cloned = []

    def fake_run(args):
        assert args[:2] == ["git", "clone"], args
        cloned.append(args[3])
        import shutil
        shutil.copytree(source, args[4])
        return ""

    monkeypatch.setattr(skills, "_run", fake_run)
    root = _signal_project(tmp_path, "screen", {"src/App.jsx": "export const App = () => <b>Hi</b>;\n"})
    report = adopt.survey(root)
    assert cloned == ["https://github.com/anthropics/knowledge-work-plugins"]
    assert [item["id"] for item in report["proposed_skills"]] == ["kwp:design/ux-copy"]
    assert "src/App.jsx" in report["proposed_skills"][0]["reason"]
    # The copy now exists, so a second survey fetches nothing.
    assert adopt.survey(root)["proposed_skills"] == report["proposed_skills"] and len(cloned) == 1


def test_default_catalogue_gives_shared_state_foragents_own_state_design(tmp_path, capsys, monkeypatch):
    from foragent import skills
    from foragent.skills import FORAGENT_REPO

    # Default settings whose two default libraries hold no matching skill; Foragent's own skills serve.
    def empty_clone(args):
        assert args[:2] == ["git", "clone"], args
        Path(args[4]).mkdir(parents=True)
        return ""

    monkeypatch.setattr(skills, "_run", empty_clone)
    root = _signal_project(tmp_path, "state", {"src/store.py": "import threading\n\nLOCK = threading.Lock()\n"})
    found = adopt.survey(root)["proposed_skills"]
    assert [(item["id"], item["source"]) for item in found] == [("foragent:fa-state-design", FORAGENT_REPO)]
    assert "src/store.py" in found[0]["reason"]
    monkeypatch.chdir(root)
    assert cli.main(["skills", "catalog", "--json"]) == 0
    entries = {entry["id"]: entry for entry in json.loads(capsys.readouterr().out)}
    assert found[0]["license"] == entries["foragent:fa-state-design"]["license"]
    # A screen still gets Foragent's own UX copy skill.
    screen = _signal_project(tmp_path, "screen", {"src/App.tsx": "export const App = () => <b>Hi</b>;\n"})
    assert [item["id"] for item in adopt.survey(screen)["proposed_skills"]] == ["foragent:fa-ux-copy"]

    # A person's own library list replaces the default catalogue, Foragent's own skills included.
    config = Path.home() / ".config/foragent/config.yml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(yaml.safe_dump({"skill_libraries": []}), encoding="utf-8")
    assert adopt.survey(root)["proposed_skills"] == []


def test_confirmed_own_skill_reaches_the_lane_catalogue_with_added_libraries(tmp_path, capsys, monkeypatch):
    from foragent import skills
    from foragent.config import load_config
    from foragent.run_skill_catalog import available_skills

    def empty_clone(args):
        assert args[:2] == ["git", "clone"], args
        Path(args[4]).mkdir(parents=True)
        return ""

    monkeypatch.setattr(skills, "_run", empty_clone)
    addon = tmp_path / "addon"
    (addon / "skills/extra").mkdir(parents=True)
    (addon / "skills/extra/SKILL.md").write_text("---\nname: extra\ndescription: Extra\n---\nBody.\n", encoding="utf-8")
    # Both default libraries kept, one approved library added.
    config = Path.home() / ".config/foragent/config.yml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(yaml.safe_dump({"skill_libraries": [
        {"name": "knowledge-work-plugins", "repo": "https://github.com/anthropics/knowledge-work-plugins"},
        {"name": "skills", "repo": "https://github.com/anthropics/skills"},
        {"name": "addon", "repo": "https://example.test/addon", "path": str(addon)},
    ], "skill_roles": {"addon:extra": "tool"}}), encoding="utf-8")
    root = _signal_project(tmp_path, "state", {
        "src/store.py": "import threading\n\nLOCK = threading.Lock()\n",
        ".foragent.yml": "lanes:\n  app:\n    paths: [src/**]\n    skills: []\n",
    })
    found = adopt.survey(root)["proposed_skills"]
    assert [item["id"] for item in found] == ["foragent:fa-state-design"]

    monkeypatch.chdir(root)
    assert cli.main(["skills", "prefer", "foragent:fa-state-design", "--repo-root", str(root)]) == 0
    offered = [name for name, _, _ in available_skills(load_config(root), [])]
    assert offered[0] == "fa-state-design" and "extra" in offered

    # Later the person drops every default library: the confirmed skill stays offered, first.
    config.write_text(yaml.safe_dump({"skill_libraries": [
        {"name": "addon", "repo": "https://example.test/addon", "path": str(addon)},
    ], "skill_roles": {"addon:extra": "tool"}}), encoding="utf-8")
    offered = [name for name, _, _ in available_skills(load_config(root), [])]
    assert offered == ["fa-state-design", "extra"]
    capsys.readouterr()
    assert cli.main(["skills", "catalog", "--json"]) == 0
    listed = [entry["id"] for entry in json.loads(capsys.readouterr().out) if not entry["id"].startswith("pstack:")]
    assert listed == ["foragent:fa-state-design", "addon:extra"]
    assert cli.main(["skills", "install", "foragent:fa-state-design", "--repo-root", str(root)]) == 0
    assert (root / ".claude/skills/fa-state-design/SKILL.md").is_file()


@pytest.mark.parametrize("libraries", ["empty", "own", "added"])
def test_project_catalogue_is_shared_by_survey_prefer_and_lane(tmp_path, capsys, monkeypatch, libraries):
    from foragent.config import load_config
    from foragent.run_skill_catalog import available_skills

    # A cached global catalogue must not override the project's library choices.
    _skill_library(tmp_path, "https://github.com/anthropics/knowledge-work-plugins",
                   ["ux-copy", "testing-strategy"])
    local = tmp_path / "project-library"
    for name in ("ux-copy", "testing-strategy"):
        folder = local / "skills" / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Local {name}\n---\nBody.\n")
    (local / "LICENSE").write_text("SPDX-License-Identifier: MIT\n")
    entry = {"name": "project", "repo": "https://github.com/anthropics/skills", "path": str(local)}
    entries = [] if libraries == "empty" else [entry]
    if libraries == "added":
        entries.insert(0, {"name": "lib", "repo": "https://github.com/anthropics/knowledge-work-plugins",
                           "path": str(tmp_path / "library")})
    root = _signal_project(tmp_path, "screen", {
        "src/App.jsx": "export const App = () => <button>Hi</button>;\n",
        ".github/workflows/ci.yml": "jobs:\n  test:\n    timeout-minutes: 120\n    steps:\n    - run: pytest -q\n",
        ".foragent.yml": yaml.safe_dump({"skill_libraries": entries,
                                         "lanes": {"app": {"paths": ["src/**"], "skills": []}}}),
    })
    # A prefer target is independent of the caller's current project.
    monkeypatch.chdir(tmp_path)
    found = adopt.survey(root)["proposed_skills"]
    assert len(found) == (0 if libraries == "empty" else 2)
    for proposal in found:
        assert cli.main(["skills", "prefer", proposal["id"], "--repo-root", str(root)]) == 0
        offered = available_skills(load_config(root), [])
        assert any(name == proposal["name"] for name, _, _ in offered)
        if libraries == "own":
            assert (proposal["name"], str((local / "skills" / proposal["name"] / "SKILL.md").resolve())) in [
                (name, path) for name, _, path in offered
            ]
    before = (root / ".foragent.yml").read_bytes()
    # An available global skill that this project excludes cannot be preferred.
    if libraries != "added":
        assert cli.main(["skills", "prefer", "lib:ux-copy", "--repo-root", str(root)]) != 0
        assert (root / ".foragent.yml").read_bytes() == before
    capsys.readouterr()
    monkeypatch.chdir(root)
    assert cli.main(["skills", "catalog", "--json"]) == 0
    catalog = {item["id"]: item for item in json.loads(capsys.readouterr().out)}
    assert all(item["id"] in catalog and item["license"] == catalog[item["id"]]["license"] for item in found)
    if libraries != "empty":
        assert {"project:ux-copy", "project:testing-strategy"} <= catalog.keys()
    if libraries != "added":
        assert not any(identity.startswith("lib:") for identity in catalog)
    offered = available_skills(load_config(root), [])
    assert [name for name, _, _ in offered[:len(found)]] == [item["name"] for item in found]
    if libraries == "empty":
        assert offered == []
    assert load_config(root).lanes["app"]["skills"] == []


@pytest.mark.parametrize("first", ["survey", "catalog", "prefer", "lane"])
@pytest.mark.parametrize("source", ["trusted", "approved"])
def test_every_catalogue_entry_point_fetches_the_same_missing_sources(
    tmp_path, capsys, monkeypatch, first, source,
):
    from foragent import skills
    from foragent.config import load_config
    from foragent.run_skill_catalog import available_skills

    calls = []

    def clone(args):
        assert args[:2] == ["git", "clone"]
        calls.append(args[3])
        path = Path(args[4])
        folder = path / "skills/ux-copy"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("---\nname: ux-copy\ndescription: Screen copy\n---\nBody.\n")
        (path / "LICENSE").write_text("SPDX-License-Identifier: MIT\n")
        return ""

    monkeypatch.setattr(skills, "_run", clone)
    repo = "https://github.com/anthropics/skills" if source == "trusted" else "https://example.test/approved"
    settings = {"skill_libraries": [{"name": "fresh", "repo": repo}]}
    if source == "approved":
        settings["skill_roles"] = {"fresh:ux-copy": "tool"}
    root = _signal_project(tmp_path, "screen", {
        "src/App.jsx": "export const App = () => <b>Hi</b>;\n",
        ".foragent.yml": yaml.safe_dump(settings),
    })
    monkeypatch.chdir(root)
    if first == "survey":
        proposals = adopt.survey(root)["proposed_skills"]
        assert [item["id"] for item in proposals] == (["fresh:ux-copy"] if source == "trusted" else [])
    elif first == "lane":
        assert any(name == "ux-copy" for name, _, _ in available_skills(load_config(root), []))
    else:
        args = ["skills", "catalog", "--json"] if first == "catalog" else [
            "skills", "prefer", "fresh:ux-copy", "--repo-root", str(root)]
        assert cli.main(args) == 0
    assert calls == [repo]
    capsys.readouterr()
    proposals = adopt.survey(root)["proposed_skills"]
    assert [item["id"] for item in proposals] == (["fresh:ux-copy"] if source == "trusted" else [])
    assert cli.main(["skills", "prefer", "fresh:ux-copy", "--repo-root", str(root)]) == 0
    assert available_skills(load_config(root), [])[0][0] == "ux-copy"
    assert calls == [repo]


@pytest.mark.parametrize("assignment", ["style", "default"])
def test_survey_never_proposes_a_skill_that_confirmation_cannot_offer_to_lanes(tmp_path, monkeypatch, assignment):
    _skill_library(tmp_path, "https://github.com/anthropics/knowledge-work-plugins", ["ux-copy", "testing-strategy"])
    settings = {"skill_roles": {"lib:ux-copy": "style"}} if assignment == "style" else {
        "default_skills": {"docs": ["lib:ux-copy"]}}
    root = _signal_project(tmp_path, "screen", {
        "src/App.jsx": "export const App = () => <b>Hi</b>;\n",
        ".foragent.yml": yaml.safe_dump(settings),
    })
    found = adopt.survey(root)["proposed_skills"]
    assert all(item["id"] != "lib:ux-copy" for item in found)


@pytest.mark.parametrize("action", ["catalog", "prefer"])
@pytest.mark.parametrize("source", ["missing", "unavailable", "not-directory"])
def test_explicit_catalogue_commands_require_configured_sources(
    git_history, tmp_path, capsys, monkeypatch, action, source,
):
    from foragent.config import load_config
    from foragent.run_skill_catalog import available_skills

    git_history.update({
        "skills/ux-copy/SKILL.md": "---\nname: ux-copy\ndescription: Local screen copy\n---\nBody.\n",
        "LICENSE": "SPDX-License-Identifier: MIT\n",
    })
    destination = tmp_path / "library"
    if source == "not-directory":
        destination.write_text("A file occupies the configured library path.\n")
    repo = str(tmp_path / "absent.git") if source == "unavailable" else str(git_history.origin)
    root = _signal_project(tmp_path, "project", {
        ".foragent.yml": yaml.safe_dump({"skill_libraries": [
            {"name": "requested-pack", "repo": repo, "path": str(destination)},
        ]}),
    })
    monkeypatch.chdir(root)
    settings = root / ".foragent.yml"
    before = settings.read_bytes()
    argv = ["skills", "catalog", "--json"] if action == "catalog" else [
        "skills", "prefer", "requested-pack:ux-copy", "--repo-root", str(root)]
    result = cli.main(argv)
    output = capsys.readouterr()
    if source != "missing":
        assert result != 0
        assert "requested-pack" in output.err
        assert ("could not clone library" if source == "unavailable" else "not a directory") in output.err
        assert output.out == ""
        assert settings.read_bytes() == before
        return

    assert result == 0, output.err
    assert (destination / "skills/ux-copy/SKILL.md").is_file()
    if action == "catalog":
        entries = [item for item in json.loads(output.out) if item["id"].startswith("requested-pack:")]
        assert [(item["id"], item["license"]) for item in entries] == [("requested-pack:ux-copy", "MIT")]
        assert settings.read_bytes() == before
    else:
        assert load_config(root).preferred_skills == ["requested-pack:ux-copy"]
        assert available_skills(load_config(root), [])[0][2] == str(destination / "skills/ux-copy/SKILL.md")


@pytest.mark.parametrize("action", ["survey", "lane"])
@pytest.mark.parametrize("source", ["trusted", "untrusted"])
def test_optional_catalogue_discovery_keeps_available_skills_when_a_source_is_missing(
    tmp_path, monkeypatch, action, source,
):
    import subprocess
    from foragent import skills
    from foragent.config import load_config
    from foragent.run_skill_catalog import available_skills

    _skill_library(tmp_path, "https://github.com/anthropics/skills", ["ux-copy"])
    repo = ("https://github.com/anthropics/knowledge-work-plugins" if source == "trusted"
            else "https://example.test/untrusted")
    root = _signal_project(tmp_path, "project", {
        "src/App.jsx": "export const App = () => <b>Hi</b>;\n",
        ".foragent.yml": yaml.safe_dump({"skill_libraries": [
            {"name": "missing-pack", "repo": repo, "path": str(tmp_path / "missing")},
            {"name": "cached", "repo": "https://github.com/anthropics/skills", "path": str(tmp_path / "library")},
        ]}),
    })
    calls = []

    def failed_clone(args):
        assert args[:2] == ["git", "clone"]
        calls.append(args[3])
        raise subprocess.CalledProcessError(128, args, stderr="offline source")

    monkeypatch.setattr(skills, "_run", failed_clone)
    before = (root / ".foragent.yml").read_bytes()
    if action == "survey":
        assert [item["id"] for item in adopt.survey(root)["proposed_skills"]] == ["cached:ux-copy"]
    else:
        offered = available_skills(load_config(root), [])
        assert ("ux-copy", str(tmp_path / "library/skills/ux-copy/SKILL.md")) in [
            (name, path) for name, _, path in offered]
    assert calls == ([repo] if source == "trusted" else [])
    assert (root / ".foragent.yml").read_bytes() == before


@pytest.mark.parametrize("message, expected", [
    ("Fix crash (fixes #12)", {12}),
    ("Closes: #3 and resolved #4", {3, 4}),
    ("FIXED example/demo#7", {7}),
    ("close #1, fix #2, resolves #5", {1, 2, 5}),
    ("see #14 for a later flag", set()),
    ("Refs #9; prefix#10; unfixes #11", set()),
    ("fixes other/repo#8", set()),
    ("fixes#6", set()),
])
def test_closing_refs_need_a_closing_keyword_for_this_repo(message, expected):
    assert adopt.closing_refs(message, "example/demo") == expected


class GitStub:
    def __init__(self, remote="https://github.com/example/demo.git", log=""):
        self.remote, self.log = remote, log

    def _call(self, args):
        if args[:2] == ["remote", "get-url"]:
            if self.remote is None:
                raise adopt.GitError("no origin")
            return self.remote
        assert args[:2] == ["log", "HEAD"]
        return self.log


def fake_gh(monkeypatch, outputs):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        result = outputs[args[0]]
        if isinstance(result, BaseException):
            raise result
        code, stdout = result
        return adopt.subprocess.CompletedProcess(["gh", *args], code, stdout, "boom\n")

    monkeypatch.setattr(adopt.gh, "run", run)
    return calls


def test_github_marks_only_issues_closed_by_a_commit(monkeypatch):
    log = ("a" * 40 + "\0Fix crash\0Fix crash\n\nfixes #12\n\x1e\n"
           + "b" * 40 + "\0Mention\0Mention\n\nsee #14\n\x1e\n")
    issues = [{"number": 12, "title": "Crash", "url": "u12", "labels": [{"name": "bug"}]},
              {"number": 14, "title": "Flag", "url": "u14", "labels": []}]
    calls = fake_gh(monkeypatch, {"issue": (0, json.dumps(issues)),
                                  "pr": (0, json.dumps([{"number": 20, "title": "Docs", "url": "u20",
                                                         "headRefName": "docs"}]))})
    report = adopt._github(GitStub(log=log), True)
    assert report["available"] and report["repo"] == "example/demo" and not report["truncated"]
    assert report["issues"] == [
        {"number": 12, "title": "Crash", "url": "u12", "labels": ["bug"],
         "fixed_by": [{"sha": "a" * 40, "subject": "Fix crash"}]},
        {"number": 14, "title": "Flag", "url": "u14", "labels": [], "fixed_by": []},
    ]
    assert report["pulls"] == [{"number": 20, "title": "Docs", "url": "u20", "head": "docs"}]
    assert [args[:2] for args in calls] == [["issue", "list"], ["pr", "list"]]
    assert all("--jq" not in args and "open" in args for args in calls)


@pytest.mark.parametrize("outputs, reason", [
    ({"issue": FileNotFoundError()}, "gh is not installed"),
    ({"issue": adopt.subprocess.TimeoutExpired("gh", 60)}, "gh issue list timed out"),
    ({"issue": (4, "")}, "gh issue list failed: boom"),
    ({"issue": (0, "not json")}, "gh issue list returned unreadable JSON"),
    ({"issue": (0, "[]"), "pr": (0, "{}")}, "gh pr list returned unexpected JSON"),
])
def test_github_failure_is_reported_not_raised(monkeypatch, outputs, reason):
    fake_gh(monkeypatch, outputs)
    report = adopt._github(GitStub(), True)
    assert report["available"] is False and report["reason"] == reason
    assert report["issues"] == [] and report["pulls"] == []


@pytest.mark.parametrize("remote", [None, "/tmp/origin.git", "https://gitlab.com/a/b.git"])
def test_github_without_a_github_remote_never_calls_gh(monkeypatch, remote):
    calls = fake_gh(monkeypatch, {})
    report = adopt._github(GitStub(remote=remote), True)
    assert report["reason"] == "no GitHub remote" and calls == []


def test_github_full_page_is_marked_truncated(monkeypatch):
    issues = [{"number": n, "title": "t", "url": "u", "labels": []} for n in range(adopt.GH_LIST_LIMIT)]
    fake_gh(monkeypatch, {"issue": (0, json.dumps(issues)), "pr": (0, "[]")})
    assert adopt._github(GitStub(), False)["truncated"] is True


def test_docs_lists_readme_claude_and_agents_with_excerpts(tmp_path):
    (tmp_path / "README.md").write_text("\n# Demo\n\nCounts words.\n" + "x" * 400, encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_bytes(b"Keep \xff small.\n")
    (tmp_path / "notes.md").write_text("not a project doc\n", encoding="utf-8")
    docs = adopt._docs(tmp_path)
    assert [doc["path"] for doc in docs] == ["README.md", "CLAUDE.md"]
    assert docs[0]["excerpt"].startswith("# Demo Counts words. x") and len(docs[0]["excerpt"]) == 300
    assert docs[1]["excerpt"] == "Keep � small."


def test_survey_text_names_docs_and_fixed_issues(monkeypatch, tmp_path, capsys):
    report = {"kind": "existing", "hot_paths": [], "existing_setup": [], "ci": [],
              "docs": [{"path": "README.md", "excerpt": "x"}],
              "github": {"repo": "example/demo", "available": True, "reason": None, "truncated": False,
                         "issues": [{"number": 12, "title": "Crash", "url": "", "labels": [],
                                     "fixed_by": [{"sha": "a" * 40, "subject": "Fix crash"}]}],
                         "pulls": [{"number": 20, "title": "Docs", "url": "", "head": "docs"}]},
              "proposed_skills": [{"id": "lib:ux-copy", "name": "ux-copy", "source": "https://example.test/lib",
                                   "license": "MIT", "reason": "src/App.tsx is a screen"}]}
    monkeypatch.setattr(adopt, "survey", lambda root: report)
    assert cli.main(["adopt", "survey", "--repo-root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Docs read: README.md" in out
    assert "Open issues (1, 1 already fixed by a commit):" in out
    assert "#12 Crash (fixed by aaaaaaaaaaaa Fix crash)" in out
    assert "#20 Docs (docs)" in out
    assert "Proposed skill: lib:ux-copy (https://example.test/lib, MIT): src/App.tsx is a screen" in out


def test_fa_init_lane_skill_commands_keep_the_lanes_current_skills():
    """`fa lanes skills` replaces the whole list, so every example passes the current ids."""
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "skills/fa-init/SKILL.md").read_text(encoding="utf-8")
    commands = [command for command in re.findall(r"`(fa lanes skills [^`]+)`", text)]
    assert commands, "fa-init must show how to fix a skill on a reused lane"
    for command in commands:
        assert "<current-ids>" in command, f"would drop the lane's existing skills: {command}"
