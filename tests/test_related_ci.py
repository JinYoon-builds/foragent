"""Lane PR test selection (related_ci): pytest option values and the harness never narrow wrongly."""

from pathlib import Path

import pytest

from foragent import related_ci


@pytest.fixture
def root(tmp_path):
    for relative in ("tests/test_alpha.py", "tests/acceptance/test_done.py", "src/demo/alpha.py"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("command", [
    "uv run pytest -q tests/test_alpha.py --junitxml /tmp/foragent-related/report.xml",
    "uv run pytest -q --junitxml /tmp/x/report.xml -o junit_family=legacy tests/test_alpha.py",
    "uv run pytest -q tests/test_alpha.py --basetemp missing/tmp -p no:cacheprovider -k 'a/b'",
    ".venv/bin/pytest -q --rootdir . -c missing/pytest.ini tests/test_alpha.py",
])
def test_option_values_are_not_selectors(root, command):
    assert related_ci.command_paths(command, root) == ["tests/test_alpha.py"]


def test_paths_outside_the_repository_are_not_selectors(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "test_other.py"
    outside.write_text("", encoding="utf-8")
    command = f"uv run pytest -q {outside} {root / 'tests/acceptance/test_done.py'}"
    assert related_ci.command_paths(command, root) == ["tests/acceptance/test_done.py"]


def test_a_missing_named_test_path_widens(root):
    assert related_ci.command_paths("uv run pytest -q tests/test_gone.py", root) is None


@pytest.mark.parametrize("path", [
    "src/conftest.py", "src/demo/conftest.py", "src/sitecustomize.py",
    "tests/conftest.py", "tests/helpers.py", "pytest.ini", ".github/workflows/ci.yml", "pyproject.toml",
])
def test_harness_changes_are_unmappable(path):
    assert not related_ci.mappable(path)


@pytest.mark.parametrize("path", ["src/demo/alpha.py", "src/foragent/cli.py", "tests/test_alpha.py",
                                  "tests/acceptance/test_done.py"])
def test_modules_and_test_files_are_mappable(path):
    assert related_ci.mappable(path)


def test_lane_table_with_related_check_keeps_the_selection_narrow(root):
    body = ("| Done when command | Result |\n| --- | --- |\n"
            "| <code>uv run pytest -q tests/acceptance/test_done.py</code> | pass |\n"
            "| <code>uv run pytest -q tests/test_alpha.py --junitxml /tmp/r/alpha.xml</code> | pass |\n")
    selection = related_ci.select(root, ["src/demo/alpha.py"], related_ci.done_when_commands(body),
                                  "tests/acceptance/")
    assert selection == related_ci.Selection(("tests/test_alpha.py",), ("tests/acceptance/test_done.py",))


HARNESS_PROJECT = {
    "pyproject.toml": ('[project.entry-points.pytest11]\nfixture_plugin = "demo.fixture_plugin"\n\n'
                       '[tool.pytest.ini_options]\naddopts = "-p demo.marks"\n'),
    "tests/conftest.py": "import demo.fixtures\npytest_plugins = ['demo.recorder']\n\n\ndef setup():\n    import demo.patched\n",
    ".github/workflows/ci.yml": "jobs:\n  t:\n    steps:\n      - run: uv run python -m demo.runner\n",
    "src/demo/__init__.py": "",
    "src/demo/alpha.py": "def value():\n    return 1\n",
    "src/demo/marks.py": ("import importlib\n\n\ndef pytest_configure(config):\n    from demo import marks_helper\n"
                          "    marks_helper.configure()\n    importlib.import_module('demo.dynamic')\n"),
    "src/demo/recorder.py": "from . import support\n",
    "src/demo/support.py": "from demo import deep\n",
    "src/demo/deep.py": "",
    "src/demo/fixtures.py": "",
    "src/demo/patched.py": "",
    "src/demo/hooked.py": "def pytest_configure(config):\n    pass\n",
    "src/demo/runner.py": "def main():\n    from demo import lazy\n",
    "src/demo/lazy.py": "",
    "src/demo/lazy_of_lazy.py": "",
    "src/demo/not_a_hook.py": "def pytest_command(text):\n    return text\n",
    "src/demo/writes_plugin.py": 'PLUGIN = """def pytest_configure(config):\n    pass\n"""\n',
    "src/demo/writes_template.py": 'PLUGIN = """def pytest_sessionfinish(session, status):\n    {body}\n"""\n',
    "src/demo/mentions_name.py": "TEXT = 'call pytest_command first'\n",
    "src/demo/fixture_plugin.py": "import pytest\nimport demo.sub.leaf\n\n\n@pytest.fixture\ndef value():\n    return 1\n",
    "src/demo/sub/__init__.py": "",
    "src/demo/sub/leaf.py": "",
    "src/demo/marks_helper.py": "def configure():\n    from demo import marks_settings\n    return marks_settings.VALUE\n",
    "src/demo/marks_settings.py": "VALUE = 2\n",
    "src/demo/record_pkg/__init__.py": "from . import support\n\n\ndef pytest_configure(config):\n    pass\n",
    "src/demo/record_pkg/support.py": "",
    "src/demo/dynamic.py": "",
    "tests/test_alpha.py": "from demo import alpha\n",
}


@pytest.fixture
def project(tmp_path):
    for relative, text in HARNESS_PROJECT.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("module", [
    "marks",  # -p in pytest configuration
    "recorder",  # a conftest's pytest_plugins
    "support",  # imported by that plugin (relative import)
    "deep",  # imported when support is loaded
    "fixtures",  # imported when the conftest is loaded
    "hooked",  # implements a pytest hook
    "runner",  # a workflow step runs it
    "lazy",  # the runner imports it inside a function
    "writes_plugin",  # carries plugin source in a string it writes out
    "writes_template",  # the same as a template with placeholders
    "patched",  # a conftest function imports it
    "fixture_plugin",  # a pytest11 entry point with fixtures only, no hook
    "marks_helper",  # the plugin imports it inside a hook
    "marks_settings",  # that helper imports it inside a function
    "record_pkg/support",  # `from . import support` in a plugin package's __init__
    "dynamic",  # importlib.import_module("demo.dynamic")
    "sub/__init__",  # `import demo.sub.leaf` runs the demo.sub package first
])
def test_harness_modules_widen_to_the_full_suite(project, module):
    path = f"src/demo/{module}.py"
    assert path in related_ci.harness_modules(project)
    assert related_ci.select(project, [path], [], "tests/acceptance/") is None


@pytest.mark.parametrize("module", ["alpha", "lazy_of_lazy", "not_a_hook", "mentions_name"])
def test_application_modules_stay_narrow(project, module):
    """A module nothing in the harness imports, and a `pytest_`-named non-hook, are not harness."""
    assert f"src/demo/{module}.py" not in related_ci.harness_modules(project)
    assert related_ci.select(project, [f"src/demo/{module}.py"], [], "tests/acceptance/") is not None


def test_a_deleted_source_module_widens(project):
    assert related_ci.select(project, ["src/demo/gone.py"], [], "tests/acceptance/") is None


def test_removing_a_plugins_hooks_still_widens(project):
    """At the head alpha.py is plain; its base version was a plugin."""
    base = {"src/demo/alpha.py": b"def pytest_configure(config):\n    pass\n"}
    assert related_ci.select(project, ["src/demo/alpha.py"], [], "tests/acceptance/", base.get) is None


def test_this_repositorys_ci_harness_is_unmappable():
    root = Path(__file__).resolve().parents[1]
    harness = related_ci.harness_modules(root)
    for name in ("ci_skip", "ci_pytest", "ci_shard", "ci_unit_shard", "ci_base_cache", "ci_workflow",
                 "related_ci", "run_checks", "order_collect", "order_prepare_check", "order_red", "run_baseline"):
        assert f"src/foragent/{name}.py" in harness
    sources = {path.relative_to(root).as_posix() for path in (root / "src").rglob("*.py")}
    assert harness < sources, "some application modules must still narrow"
