"""AST-only loader for PWM regression fixtures, with private stdlib proxies.

Never import a production daemon. Module imports are resolved separately, then
only literal initializers and definitions are compiled; main guards are absent.
All file writes/discovery must resolve beneath the runtime scratch directory.
The only extra read target is the shipped fan configuration.
"""
import ast
import builtins
import importlib
import os
from pathlib import Path
import sys
from types import ModuleType


def load_source(path, name):
    path = Path(path).resolve()
    root = Path(os.environ["TMPDIR"]).resolve()
    config = path.parents[2] / "configs/fan-control.conf"
    tree = ast.parse(path.read_text(), filename=str(path))
    module = ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    namespace = module.__dict__

    def allowed(path, *, write=False):
        resolved = Path(path).resolve()
        if resolved.is_relative_to(root) or (not write and resolved == config):
            return resolved
        raise AssertionError("non-fixture file IO refused")

    def fixture_open(path, mode="r", *args, **kwargs):
        return builtins.open(allowed(path, write=any(x in mode for x in "wax+")), mode, *args, **kwargs)

    proxies = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for item in node.names:
                value = importlib.import_module(item.name)
                # Test monkeypatches must never alter the process-wide module.
                proxy = ModuleType(item.name)
                proxy.__dict__.update(vars(value))
                proxies[item.name] = proxy
                namespace[item.asname or item.name] = proxy
        elif isinstance(node, ast.ImportFrom):
            value = importlib.import_module(node.module)
            for item in node.names:
                namespace[item.asname or item.name] = getattr(value, item.name)
    if "glob" in proxies:
        original_glob = proxies["glob"].glob
        proxies["glob"].glob = lambda pattern: original_glob(str(allowed(pattern)))
    if "signal" in proxies:
        proxies["signal"].signal = lambda *args: (_ for _ in ()).throw(AssertionError("unmocked signal registration"))
    namespace["__builtins__"] = dict(vars(builtins), open=fixture_open)
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return module
