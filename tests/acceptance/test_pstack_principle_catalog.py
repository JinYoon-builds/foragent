"""Order acceptance: a selected pstack library is discoverable and installed on demand."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from foragent import cli, order
from test_skills_autosync import cli_exit, isolated_home, write_global_config  # noqa: F401


APPROVED = {
    'principle-attack-the-premise',
    'principle-boundary-discipline',
    'principle-build-the-lever',
    'principle-encode-lessons-in-structure',
    'principle-exhaust-the-design-space',
    'principle-experience-first',
    'principle-explain-the-number',
    'principle-fix-root-causes',
    'principle-foundational-thinking',
    'principle-guard-the-context-window',
    'principle-laziness-protocol',
    'principle-make-operations-idempotent',
    'principle-migrate-callers-then-delete-legacy-apis',
    'principle-minimize-reader-load',
    'principle-model-the-domain',
    'principle-never-block-on-the-human',
    'principle-outcome-oriented-execution',
    'principle-prove-it-works',
    'principle-redesign-from-first-principles',
    'principle-separate-before-serializing-shared-state',
    'principle-sequence-verifiable-units',
    'principle-subtract-before-you-add',
    'principle-test-behavior-not-implementation',
    'principle-type-system-discipline',
}


EXISTING = {
    "principle-fix-root-causes": "a94c87b27cc9be76709ffd0eb2d1f21745ed77d3888a537c804e874290f050e4",
    "principle-prove-it-works": "136d83b2cf20c5a4c0a94975a6a680b9d4662c774f86a2160946e8524b5747a7",
}


def test_catalog_exposes_portable_principles_with_license_and_installs_only_selected_skill(
    tmp_path, monkeypatch, capsys, isolated_home,
):
    source = Path(order.__file__).resolve().parents[2]
    root = tmp_path / "project"
    root.mkdir()
    shutil.copytree(source / "skills", root / "skills")
    shutil.copy2(source / ".foragent.yml", root / ".foragent.yml")
    for name in ("LICENSE-MIT", "THIRD_PARTY_NOTICES.md"):
        if (source / name).exists():
            shutil.copy2(source / name, root / name)
    write_global_config({"skill_libraries": []})
    monkeypatch.chdir(root)
    capsys.readouterr()
    assert cli_exit(cli.main, ["skills", "catalog", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    principles = {row["name"]: row for row in rows if row["name"].startswith("principle-")}
    assert set(principles) == APPROVED, "The approved pinned pstack principle set must be discoverable"
    for name in ("principle-fix-root-causes", "principle-prove-it-works",
                 "principle-test-behavior-not-implementation", "principle-never-block-on-the-human"):
        assert name in principles
    assert not ({"poteto-mode", "setup-pstack", "arena", "swarm"} & {row["name"] for row in rows})
    assert all(row["license"] == "MIT" and not row["runs_scripts"] for row in principles.values())
    assert not (root / ".claude/skills").exists(), "Catalog discovery must not auto-install the library"
    chosen = principles["principle-test-behavior-not-implementation"]
    foreign = tmp_path / "foreign"
    duplicate = foreign / "skills" / chosen["name"]
    duplicate.mkdir(parents=True)
    (duplicate / "SKILL.md").write_text(
        f'---\nname: {chosen["name"]}\ndescription: A distinct global fixture\n---\nFOREIGN Ω89\n')
    (foreign / "LICENSE").write_text("SPDX-License-Identifier: MIT\n")
    write_global_config({"skill_libraries": [{"name": "foreign", "repo": str(foreign), "path": str(foreign)}]})
    assert cli_exit(cli.main, ["skills", "catalog", "--json"]) == 0
    combined = json.loads(capsys.readouterr().out)
    matching = [row for row in combined if row["name"] == chosen["name"]]
    assert len(matching) == 2 and len({row["id"] for row in matching}) == 2
    assert any(row["id"] == chosen["id"] for row in matching), "The advertised project ID stays stable"
    for _ in range(2):
        assert cli_exit(cli.main, ["skills", "install", chosen["id"], "--repo-root", str(root)]) == 0
    installed = root / ".claude/skills"
    assert sorted(path.name for path in installed.iterdir()) == [chosen["name"]]
    content = (installed / chosen["name"] / "SKILL.md").read_text()
    assert "name: principle-test-behavior-not-implementation" in content
    assert "FOREIGN Ω89" not in content, "An exact catalog ID cannot silently switch sources"
    notices = "\n".join(path.read_text() for path in root.rglob("*")
                        if path.is_file() and (path.name.startswith("LICENSE") or "NOTICE" in path.name))
    for value in ("Lauren Tan", "MIT", "github.com/cursor/plugins", "9b93e81fb2921b9263d9463c15bafeabfe3903aa"):
        assert value in notices, f"Vendored library lacks attribution fact {value}"


def test_two_already_imported_principles_stay_byte_identical():
    source = Path(order.__file__).resolve().parents[2]
    for name, expected in EXISTING.items():
        assert hashlib.sha256((source / "skills" / name / "SKILL.md").read_bytes()).hexdigest() == expected


def test_sync_does_not_pull_a_local_vendored_library_or_its_parent_checkout(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    vendor = root / "vendor"
    skill = vendor / "skills/demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: A pinned local fixture\n---\nPinned bytes.\n")
    (vendor / "LICENSE").write_text("SPDX-License-Identifier: MIT\n")
    (root / ".git").mkdir()
    (root / ".foragent.yml").write_text("skill_libraries: []\n")
    write_global_config({"skill_libraries": [{"name": "vendor", "repo": str(vendor), "path": str(vendor)}]})
    monkeypatch.chdir(root)
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    pulls = []

    def transport(argv, **kwargs):
        if "pull" in argv:
            pulls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", transport)
    assert cli_exit(cli.main, ["skills", "sync"]) == 0, capsys.readouterr().err
    assert not pulls, "Syncing a local library must not pull the enclosing project's origin"
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before
