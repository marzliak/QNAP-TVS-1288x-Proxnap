"""Guarded installed entrypoints; reusable implementations live in this package.

The byte-preserved PWM module is internal. Installed/console entrypoints always
pass through the chassis guard here. No module is imported until after it.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

from .common import require_supported_model

MODULES = {
    "fan-control": "fan_control",
    "lcd": "lcd",
    "copy-update": "copy_update",
    "copy-button": "copy_update",
    "power-button": "power_button",
    "status-led": "status_led",
    "panel-led": "panel_led",
    "panel-breath": "panel_led",
    "disk-leds": "disk_leds",
    "disk-led-monitor": "disk_leds",
    "blue-temp-breath": "blue_temp_breath",
    "metrics": "metrics",
}


def dispatch(name: str, argv: list[str]) -> int:
    if name not in MODULES:
        raise ValueError(f"unknown Proxnap entrypoint: {name}")
    # Config checking/editing has no hardware action. Every other installed
    # command refuses unsupported identity before import, files or subprocesses.
    config_only = name == "fan-control" and any(x in argv for x in ("--check-config", "--set-mode"))
    if not config_only and not require_supported_model():
        return 4
    args = ["--safe" if x == "--failsafe" else x for x in argv] if name == "fan-control" else list(argv)
    if name == "lcd":
        if "--off" in args:
            args.remove("--off")
            args.append("off")
        elif not any(x in args for x in ("run", "once", "off", "-h", "--help")):
            args.append("run")
    module = importlib.import_module(f"proxnap.{MODULES[name]}")
    previous = sys.argv
    try:
        sys.argv = [f"proxnap-{name}", *args]
        return module.main()
    finally:
        sys.argv = previous


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python -m proxnap.runtime ENTRYPOINT [ARGS]", file=sys.stderr)
        return 2
    return dispatch(sys.argv[1], sys.argv[2:])


def installed_main() -> int:
    return dispatch(Path(sys.argv[0]).name.removeprefix("proxnap-"), sys.argv[1:])


def fan_main() -> int:
    return dispatch("fan-control", sys.argv[1:])


def lcd_main() -> int:
    return dispatch("lcd", sys.argv[1:])


def status_main() -> int:
    return dispatch("status-led", sys.argv[1:])


def panel_main() -> int:
    return dispatch("panel-led", sys.argv[1:])


def disk_main() -> int:
    return dispatch("disk-leds", sys.argv[1:])


def copy_main() -> int:
    return dispatch("copy-update", sys.argv[1:])


def power_main() -> int:
    return dispatch("power-button", sys.argv[1:])


def metrics_main() -> int:
    return dispatch("metrics", sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
