#!/usr/bin/env python3
"""Install the shared file payload; staging cannot invoke services or hardware.

A staging root must be below the explicit TMPDIR, never / or a symlink. The
live installer passes --live-install explicitly after its chassis preflight.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from proxnap.common import require_supported_model, set_config_value  # noqa: E402

WRAPPERS = ("fan-control", "lcd", "copy-update", "copy-button", "power-button",
            "status-led", "panel-led", "panel-breath", "disk-leds",
            "disk-led-monitor", "blue-temp-breath", "metrics")


def validate_fan_config(path: Path):
    """Use the exact pure config definitions without importing a live daemon."""
    source = PROJECT / "src/proxnap/fan_control.py"
    tree = ast.parse(source.read_text(), filename=str(source))
    names = {"FanGroup", "Settings", "clamp", "parse_bool", "parse_float",
             "parse_optional_float", "parse_config_text", "read_config", "parse_curve",
             "parse_optional_duty", "profile_curves", "validate_raw_config",
             "settings_from_config", "settings_from_raw"}
    module = ModuleType("proxnap_payload_config_check")
    previous = sys.modules.get(module.__name__)
    sys.modules[module.__name__] = module
    def config_open(target, mode="r", *args, **kwargs):
        if Path(target).resolve() != path.resolve() or mode != "r":
            raise ValueError("configuration validator refused non-config IO")
        return open(target, mode, *args, **kwargs)
    namespace = module.__dict__
    namespace.update(dataclass=dataclass, math=math, open=config_open)
    nodes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
            nodes.append(node)
        elif isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) and t.id.isupper() for t in node.targets):
            ast.literal_eval(node.value)
            nodes.append(node)
    try:
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "<payload-config-only>", "exec"), namespace)
        namespace["settings_from_config"](str(path))
    finally:
        if previous is None:
            sys.modules.pop(module.__name__, None)
        else:
            sys.modules[module.__name__] = previous


def live_preflight():
    """Refuse wrong chassis and active/failed controllers before changing files.

    This is a fresh-install payload, not an in-place live-upgrade transaction.
    A service stop/thermal recovery procedure is a separate operator decision.
    """
    if os.geteuid() != 0:
        raise ValueError("live payload requires root")
    if not require_supported_model():
        raise ValueError("live payload requires a verified supported chassis")
    units = ("proxnap-fan-control", "proxnap-lcd", "proxnap-status-led",
             "proxnap-copy-button", "proxnap-copy-update", "proxnap-power-button",
             "proxnap-disk-leds", "proxnap-disk-led-monitor", "proxnap-panel-breath",
             "proxnap-panel-static", "proxnap-blue-temp-breath", "proxnap-metrics")
    for unit in units:
        state = subprocess.run(["systemctl", "is-active", unit + ".service"],
                               capture_output=True, text=True, timeout=10)
        if state.stdout.strip() not in ("inactive", "unknown") or state.returncode not in (3, 4) or state.stderr.strip():
            raise ValueError("live payload refuses active, failed or unverified service state: " + unit)


def payload(root: Path, *, live=False, dry_run=False, options=()):
    root = root.absolute()
    if live:
        if root != Path("/"):
            raise ValueError("live payload requires the filesystem root")
        live_preflight()
    else:
        scratch = Path(os.environ["TMPDIR"]).resolve()
        if root.resolve() != root or root == scratch or not root.is_relative_to(scratch):
            raise ValueError("staging root must be a non-symlink child of TMPDIR")

    def destination(relative):
        result = root / relative
        current = result
        while current != root:
            if current.is_symlink():
                raise ValueError("symlink in payload destination")
            current = current.parent
        return result

    config = destination("etc/proxnap/fan-control.conf")
    validate_fan_config(config if config.exists() else PROJECT / "configs/fan-control.conf")
    files = []
    for source in sorted((PROJECT / "src/proxnap").glob("*.py")):
        files.append((source, destination("opt/proxnap/proxnap/" + source.name), 0o644, False))
    for source in sorted((PROJECT / "configs").iterdir()):
        name = "metrics.env" if source.name == "metrics.env.example" else source.name
        files.append((source, destination("etc/proxnap/" + name), 0o600 if name == "metrics.env" else 0o644, True))
    for source in sorted((PROJECT / "systemd").glob("*.service")):
        files.append((source, destination("etc/systemd/system/" + source.name), 0o644, False))
    for name in WRAPPERS:
        files.append((PROJECT / "scripts/proxnap-entrypoint", destination("usr/local/sbin/proxnap-" + name), 0o755, False))
    # Preflight every destination before the first write.
    for source, target, mode, keep in files:
        if not source.is_file():
            raise ValueError("missing payload input: " + source.name)
        print(("KEEP " if keep and target.exists() else "INSTALL ") + str(target))
    if dry_run:
        return
    for source, target, mode, keep in files:
        if keep and target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(mode)
    flags = set(options)
    values = {
        "copy-button.conf": {
            "enabled": "--enable-copy-button" in flags,
            "short_refresh_enabled": "--allow-apt-refresh" in flags,
            "upgrade_enabled": "--allow-dist-upgrade" in flags,
            "reboot_enabled": "--allow-reboot" in flags,
            "poweroff_enabled": "--allow-poweroff" in flags,
        },
        "power-button.conf": {
            "enabled": "--enable-power-button" in flags,
            "reboot_enabled": "--allow-reboot" in flags,
            "poweroff_enabled": "--allow-poweroff" in flags,
        },
    }
    for config, keys in values.items():
        for key, enabled in keys.items():
            set_config_value(str(destination("etc/proxnap/" + config)), key, str(enabled).lower())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--destdir", type=Path, required=True)
    parser.add_argument("--live-install", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args, options = parser.parse_known_args()
    payload(args.destdir, live=args.live_install, dry_run=args.dry_run, options=options)


if __name__ == "__main__":
    main()
