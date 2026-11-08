"""Pure distribution tests; fixtures cannot invoke hardware or maintenance."""
import ast
import hashlib
import json
from pathlib import Path
import sys
import types

import pytest


SOURCE = Path(__file__).resolve().parents[1] / 'scripts/build_standalone.py'


def builder():
    tree = ast.parse(SOURCE.read_text())
    selected = [node for node in tree.body
                if (isinstance(node, ast.FunctionDef) and node.name == 'build_source')
                or (isinstance(node, ast.Assign)
                    and any(isinstance(name, ast.Name) and name.id == 'COMPONENTS'
                            for name in node.targets))]
    namespace = {'ast': ast, 'hashlib': hashlib, 'json': json}
    exec(compile(ast.Module(body=selected, type_ignores=[]), '<pure-builder>', 'exec'), namespace)
    return namespace['build_source']


@pytest.mark.parametrize('component', ['lcd', 'copy-update'])
def test_canonical_utility_is_embedded_once_and_runs_in_private_process_namespace(component, monkeypatch):
    build = builder()
    utility = 'def send_status_notice(name, op):\n    return (name, op)\n'
    fixture = ('from proxnap.status_ipc import send_status_notice\n'
               'observed = (send_status_notice("fixture", "status"), __name__)\n')
    # Do not disturb the test process's real package namespace.
    fake_sys = types.ModuleType('sys')
    fake_sys.modules = {}
    original_import = __import__
    def confined_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'sys':
            return fake_sys
        if name == 'proxnap.status_ipc':
            return fake_sys.modules[name]
        if name == 'types':
            return types
        raise AssertionError('fixture import outside allowlist: ' + name)
    builtins = dict(vars(__import__('builtins')))
    builtins['__import__'] = confined_import
    namespace = {'__name__': '__main__', '__builtins__': builtins}
    payload = build(component, fixture, utility)
    exec(compile(payload, '<distribution-fixture>', 'exec'), namespace)
    assert namespace['observed'] == (('fixture', 'status'), '__main__')
    assert payload == build(component, fixture, utility)
    assert 'proxnap.status_ipc' in fake_sys.modules
    assert original_import('sys') is sys


@pytest.mark.parametrize('component', ['lcd', 'copy-update'])
def test_real_canonical_sources_compile_and_record_exact_input_identities(component):
    root = SOURCE.parents[1] / 'src/proxnap'
    source_name = 'lcd.py' if component == 'lcd' else 'copy_update.py'
    source = (root / source_name).read_text()
    utility = (root / 'status_ipc.py').read_text()
    payload = builder()(component, source, utility)
    ast.parse(payload)
    metadata = json.loads(payload.decode().splitlines()[2].split(': ', 1)[1])
    assert metadata == {'component': component,
                        'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
                        'status_ipc_sha256': hashlib.sha256(utility.encode()).hexdigest()}


def test_rejects_unrecognized_components_and_invalid_input():
    build = builder()
    with pytest.raises(ValueError):
        build('power-button', 'pass', 'pass')
    with pytest.raises(SyntaxError):
        build('lcd', 'not valid python !', 'pass')
    with pytest.raises(SyntaxError):
        build('copy-update', 'pass', 'not valid python !')
