#!/usr/bin/env python3
"""Build one flat legacy entrypoint from canonical source, without installing it.

The normal package installer remains authoritative for fresh installations. This
builder supports bounded updates of legacy flat LCD/Copy scripts without changing
system Python paths, units or interpreter configuration. Output is deterministic;
the shared utility remains one canonical source, embedded in each distribution.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path

COMPONENTS = {'lcd': 'lcd.py', 'copy-update': 'copy_update.py'}


def build_source(component: str, source: str, utility: str) -> bytes:
    if component not in COMPONENTS:
        raise ValueError('unsupported standalone component')
    ast.parse(source)
    ast.parse(utility)
    identities = {'component': component,
                  'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
                  'status_ipc_sha256': hashlib.sha256(utility.encode()).hexdigest()}
    header = '#!/usr/bin/env python3\n# Generated distribution, not another source owner.\n'
    header += '# Canonical inputs: ' + json.dumps(identities, sort_keys=True) + '\n'
    bootstrap = (
        'import sys as _proxnap_boot_sys\n'
        'import types as _proxnap_boot_types\n'
        '_proxnap_boot_pkg = _proxnap_boot_sys.modules.get("proxnap")\n'
        'if _proxnap_boot_pkg is None:\n'
        '    _proxnap_boot_pkg = _proxnap_boot_types.ModuleType("proxnap")\n'
        '    _proxnap_boot_pkg.__path__ = []\n'
        '    _proxnap_boot_sys.modules["proxnap"] = _proxnap_boot_pkg\n'
        '_proxnap_boot_ipc = _proxnap_boot_types.ModuleType("proxnap.status_ipc")\n'
        '_proxnap_boot_sys.modules["proxnap.status_ipc"] = _proxnap_boot_ipc\n'
        '_proxnap_boot_pkg.status_ipc = _proxnap_boot_ipc\n'
    )
    bootstrap += 'exec(compile(' + repr(utility) + ', "<proxnap/status_ipc.py>", "exec"), _proxnap_boot_ipc.__dict__)\n'
    bootstrap += 'exec(compile(' + repr(source) + ', "<proxnap/' + COMPONENTS[component] + '>", "exec"), globals())\n'
    result = (header + bootstrap).encode()
    ast.parse(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('component', choices=sorted(COMPONENTS))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / 'src/proxnap'
    result = build_source(args.component, (root / COMPONENTS[args.component]).read_text(),
                          (root / 'status_ipc.py').read_text())
    import sys
    sys.stdout.buffer.write(result)


if __name__ == '__main__':
    main()
