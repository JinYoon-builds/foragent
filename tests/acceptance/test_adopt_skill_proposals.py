"""Acceptance: `fa adopt survey --json` proposes conditional skills from the project (#1653).

Layer 2 of the skill layers: the survey reads the code and proposes catalogue skills,
each with a reason tied to evidence in that project, its source and its license facts.
A screen gets UX copy, shared state gets state design, heavy tests get a testing
strategy. A proposal is only a proposal: nothing is attached until a person confirms.

Which catalogue skill answers each concern, and the wording of reasons, belong to the lane.
"""

import json
from pathlib import Path

import yaml

from foragent import cli

from conftest import GitHistory, git_command

ROOT = Path(__file__).resolve().parents[2]
MIT = "SPDX-License-Identifier: MIT\n"
TRUSTED_KWP = "https://github.com/anthropics/knowledge-work-plugins"
TRUSTED_SKILLS = "https://github.com/anthropics/skills"
UNTRUSTED = "https://github.com/unknown-publisher/skills"


def write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def skill(name: str, description: str) -> str:
    return f"---\nname: {name}\ndescription: {json.dumps(description)}\n---\nGuidance.\n"


def libraries(tmp_path: Path) -> None:
    """Two trusted libraries, and an untrusted one listed first that offers same-named skills."""
    kwp, anthropic, community = tmp_path / "kwp", tmp_path / "anthropic", tmp_path / "community"
    write(kwp, {
        "LICENSE": MIT,
        "design/skills/ux-copy/SKILL.md": skill(
            "ux-copy", "Write interface copy: button labels, error messages and onboarding text."),
        "engineering/skills/state-design/SKILL.md": skill(
            "state-design", "Design shared state: one writer per piece of state, races, locks and atomic updates."),
        "engineering/skills/testing-strategy/SKILL.md": skill(
            "testing-strategy", "Plan a testing strategy for a large or slow test suite: layers, speed and coverage."),
        "finance/skills/reconciliation/SKILL.md": skill(
            "reconciliation", "Reconcile ledger accounts at month end."),
        "sales/skills/call-prep/SKILL.md": skill("call-prep", "Prepare for a sales call."),
    })
    write(anthropic, {"LICENSE": MIT, "skills/pdf/SKILL.md": skill("pdf", "Read and fill PDF forms.")})
    write(community, {
        "LICENSE": MIT,
        "skills/ux-copy/SKILL.md": skill("ux-copy", "Interface copy for every screen and button."),
        "skills/state-design/SKILL.md": skill("state-design", "Shared state, locks and concurrency safety."),
        "skills/testing-strategy/SKILL.md": skill("testing-strategy", "Testing strategy for slow, heavy test suites."),
    })
    config = Path.home() / ".config/foragent/config.yml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(yaml.safe_dump({"skill_libraries": [
        {"name": "community", "repo": UNTRUSTED, "path": str(community)},
        {"name": "knowledge-work-plugins", "repo": TRUSTED_KWP, "path": str(kwp)},
        {"name": "skills", "repo": TRUSTED_SKILLS, "path": str(anthropic)},
    ]}), encoding="utf-8")


def project(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    """A clone whose one commit holds the README and ``files``."""
    origin, clone = tmp_path / f"{name}.git", tmp_path / name
    git_command(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    GitHistory(origin, clone).update({"README.md": f"# {name}\n", **files})
    git_command(tmp_path, "clone", str(origin), str(clone))
    return clone


def screen_project(tmp_path: Path) -> Path:
    return project(tmp_path, "screen", {
        "package.json": json.dumps({"name": "shop", "dependencies": {"react": "18.3.1", "react-dom": "18.3.1"}}),
        "src/App.jsx": "export default function App() {\n  return <button>Buy now</button>;\n}\n",
        "src/Checkout.jsx": "export function Checkout() {\n  return <p>Your cart is empty</p>;\n}\n",
        # Lane setup exists already; a proposal must not change it.
        ".foragent.yml": "lanes:\n  app:\n    paths:\n    - src/**\n    skills: []\n",
        ".claude/agents/fa-app.md": "---\nname: fa-app\nskills: []\n---\nApp lane.\n",
    })


def state_project(tmp_path: Path) -> Path:
    return project(tmp_path, "state", {
        "pyproject.toml": "[project]\nname = \"ledger\"\nversion = \"0.1.0\"\n",
        "src/ledger/store.py": (
            "import fcntl\nimport sqlite3\nimport threading\n\n"
            "_lock = threading.Lock()\n_balances: dict[str, int] = {}\n\n\n"
            "def credit(account: str, amount: int, path: str) -> None:\n"
            "    with _lock:\n        _balances[account] = _balances.get(account, 0) + amount\n"
            "    with open(path + '.lock', 'w') as handle:\n"
            "        fcntl.flock(handle, fcntl.LOCK_EX)\n"
            "        sqlite3.connect(path).execute('UPDATE accounts SET balance = balance + ?', (amount,))\n"
        ),
        "src/ledger/workers.py": (
            "from concurrent.futures import ThreadPoolExecutor\nfrom multiprocessing import Process, Queue\n\n"
            "from ledger.store import credit\n\n\n"
            "def run(jobs, path):\n"
            "    with ThreadPoolExecutor(max_workers=8) as pool:\n"
            "        for account, amount in jobs:\n            pool.submit(credit, account, amount, path)\n"
            "    queue = Queue()\n    workers = [Process(target=credit, args=('a', 1, path)) for _ in range(4)]\n"
            "    for worker in workers:\n        worker.start()\n"
        ),
    })


def heavy_tests_project(tmp_path: Path) -> Path:
    files = {
        "pyproject.toml": ("[project]\nname = \"calc\"\nversion = \"0.1.0\"\n\n[tool.pytest.ini_options]\n"
                           "markers = [\"slow: takes seconds\"]\ntimeout = 1800\n"),
        "src/calc.py": "def add(a, b):\n    return a + b\n",
        ".github/workflows/ci.yml": ("on: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    timeout-minutes: 120\n"
                                    "    steps:\n      - run: uv run pytest -q\n"),
    }
    for module in range(80):
        cases = "".join(
            f"\n\n@pytest.mark.slow\ndef test_case_{case}():\n    time.sleep(3)\n    assert add({case}, {module}) == {case + module}\n"
            for case in range(25))
        files[f"tests/integration/test_module_{module:02d}.py"] = f"import time\n\nimport pytest\n\nfrom calc import add\n{cases}"
    return project(tmp_path, "heavy-tests", files)


def plain_project(tmp_path: Path) -> Path:
    return project(tmp_path, "plain", {
        "pyproject.toml": "[project]\nname = \"hello\"\nversion = \"0.1.0\"\n",
        "src/hello.py": "def greet(name):\n    return f'Hello, {name}'\n",
        "tests/test_hello.py": ("from hello import greet\n\n\ndef test_greet():\n    assert greet('a') == 'Hello, a'\n\n\n"
                                "def test_greet_empty():\n    assert greet('') == 'Hello, '\n"),
    })


def survey(root: Path, capsys, monkeypatch) -> dict:
    monkeypatch.chdir(root)
    assert cli.main(["adopt", "survey", "--repo-root", str(root), "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def catalog(root: Path, capsys, monkeypatch) -> dict[str, dict]:
    monkeypatch.chdir(root)
    assert cli.main(["skills", "catalog", "--json"]) == 0
    return {entry["id"]: entry for entry in json.loads(capsys.readouterr().out)}


def proposals(report: dict) -> list[dict]:
    assert "proposed_skills" in report, "The survey must list its conditional skill proposals"
    found = report["proposed_skills"]
    assert isinstance(found, list)
    return found


def assert_catalogue_facts(found: list[dict], entries: dict[str, dict]) -> None:
    ids = [item["id"] for item in found]
    assert len(ids) == len(set(ids)), f"Propose each skill once: {ids}"
    repos = {"kwp": TRUSTED_KWP, "skills": TRUSTED_SKILLS, "community": UNTRUSTED}
    for item in found:
        assert item["id"] in entries, f"{item['id']} must come from `fa skills catalog`"
        prefix = item["id"].split(":", 1)[0]
        assert prefix != "community" and item.get("source") != UNTRUSTED, (
            f"{item['id']} comes from an untrusted source; propose only trusted sources")
        if prefix in repos:
            assert item.get("source") == repos[prefix], f"{item['id']} must name its source repository"
        else:
            assert isinstance(item.get("source"), str) and item["source"], f"{item['id']} must name its source"
        assert item.get("license") == entries[item["id"]]["license"], (
            f"{item['id']} must carry the catalogue's license facts")
        assert isinstance(item.get("reason"), str) and item["reason"].strip(), f"{item['id']} needs a reason"


def concern_skill(found: list[dict], entries: dict[str, dict], concern: str, evidence: tuple[str, ...]) -> str:
    """The proposed skill about ``concern`` whose reason points at this project's evidence."""
    for item in found:
        entry = entries[item["id"]]
        about = " ".join((entry["id"], entry["name"], entry["description"])).casefold()
        if concern in about and any(path in item["reason"] for path in evidence):
            return item["id"]
    raise AssertionError(f"Propose a {concern!r} skill with a reason naming one of {evidence}: {found}")


def test_each_signal_gets_its_own_skill_with_evidence(tmp_path, capsys, monkeypatch):
    libraries(tmp_path)
    cases = (
        (screen_project(tmp_path), "copy", ("package.json", "src/App.jsx", "src/Checkout.jsx", "react")),
        (state_project(tmp_path), "state", ("src/ledger/store.py", "src/ledger/workers.py", "src/ledger")),
        (heavy_tests_project(tmp_path), "test", ("tests/integration", "tests/", ".github/workflows/ci.yml", "pyproject.toml")),
    )
    chosen = {}
    for root, concern, evidence in cases:
        entries = catalog(root, capsys, monkeypatch)
        found = proposals(survey(root, capsys, monkeypatch))
        assert found, f"{root.name}: a project with this signal must get a proposal"
        assert_catalogue_facts(found, entries)
        for item in found:
            assert any(token in item["reason"] for token in evidence), (
                f"{root.name}: the reason for {item['id']} must point at this project's evidence {evidence}")
        chosen[root.name] = (concern_skill(found, entries, concern, evidence), {item["id"] for item in found})
    for name, (skill_id, _) in chosen.items():
        for other, (_, others) in chosen.items():
            if other != name:
                assert skill_id not in others, (
                    f"{skill_id} answers {name}'s evidence; {other} has no such evidence and must not get it")


def test_plain_project_gets_no_proposals(tmp_path, capsys, monkeypatch):
    libraries(tmp_path)
    root = plain_project(tmp_path)
    assert proposals(survey(root, capsys, monkeypatch)) == []


def test_proposals_attach_nothing(tmp_path, capsys, monkeypatch):
    libraries(tmp_path)
    root = screen_project(tmp_path)
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file() and ".git" not in path.parts}
    found = proposals(survey(root, capsys, monkeypatch))
    assert found, "The screen project must get a proposal"
    after = {path: path.read_bytes() for path in root.rglob("*") if path.is_file() and ".git" not in path.parts}
    assert after == before, "A proposal must not attach skills to lanes, agents or the project before confirmation"
    assert git_command(root, "status", "--porcelain") == ""
    lanes = yaml.safe_load((root / ".foragent.yml").read_text(encoding="utf-8"))["lanes"]
    assert lanes["app"]["skills"] == []


def test_first_run_shows_the_proposals():
    text = (ROOT / "skills/fa-init/SKILL.md").read_text(encoding="utf-8")
    assert "proposed_skills" in text, "fa-init must show the survey's skill proposals for a person to confirm"
