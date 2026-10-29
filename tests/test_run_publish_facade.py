"""The run_publish split keeps foragent.run_publish.<name> patches effective (#560)."""

import ast
from pathlib import Path

from foragent import run_publish, run_publish_ci

PACKAGE = Path(run_publish.__file__).parent
SPLIT = sorted(PACKAGE.glob("run_publish_*.py"))


def _facade_globals() -> set[str]:
    return {name for name in vars(run_publish) if not name.startswith("__")}


def _bare_global_loads(path: Path, names: set[str]) -> list[str]:
    """Names of facade globals read directly inside function bodies (not annotations/decorators)."""
    found = []
    for fn in ast.parse(path.read_text(encoding="utf-8")).body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        skip = {id(node) for d in fn.decorator_list for node in ast.walk(d)}
        local = set()
        for node in ast.walk(fn):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.returns:
                    skip |= {id(x) for x in ast.walk(node.returns)}
                for arg in node.args.args + node.args.kwonlyargs + node.args.posonlyargs:
                    local.add(arg.arg)
                    if arg.annotation:
                        skip |= {id(x) for x in ast.walk(arg.annotation)}
                if node is not fn:
                    local.add(node.name)
            elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
                local.add(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                local |= {alias.asname or alias.name for alias in node.names}
            elif isinstance(node, ast.ExceptHandler) and node.name:
                local.add(node.name)
        found += [f"{path.name}:{fn.name}:{node.id}" for node in ast.walk(fn)
                  if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                  and node.id in names and node.id not in local and id(node) not in skip]
    return found


def test_split_modules_read_every_facade_name_through_the_facade():
    assert SPLIT, "run_publish must be split into run_publish_*.py modules"
    names = _facade_globals() - {"run_publish", "annotations", "TYPE_CHECKING"}
    bare = [hit for path in SPLIT for hit in _bare_global_loads(path, names)]
    assert bare == []


def test_patching_the_facade_reaches_moved_code(monkeypatch):
    calls = []
    monkeypatch.setattr(run_publish, "flag", lambda ctx, n, reason, summary, details=None: calls.append(reason))
    assert run_publish_ci._hold_changed_head(7, object(), "link") == "held"
    assert calls == [run_publish.Reason.RISK_HIGH]
