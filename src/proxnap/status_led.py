from __future__ import annotations

import argparse
import glob
import os
import signal
import subprocess
import time

from .common import (
    parse_float,
    read_config,
    read_text,
    require_supported_model,
    set_led,
    valid_temp,
)
from .fan_control import FAN_FAULT_FLAG

DEFAULT_CONFIG = "/etc/proxnap/status-led.conf"
RUNNING = True


def stop(_signum=None, _frame=None) -> None:
    global RUNNING
    RUNNING = False


def max_temp() -> float | None:
    values: list[float] = []
    for path in glob.glob("/sys/class/hwmon/hwmon*/temp*_input"):
        value = valid_temp(path)
        if value is not None:
            values.append(value)
    return max(values) if values else None


def failed_units() -> list[str]:
    try:
        result = subprocess.run(
            ["systemctl", "--failed", "--no-legend", "--plain"],
            check=False, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.split()[0] for line in result.stdout.splitlines() if line.split()]


def reasons(config_path: str) -> list[str]:
    config = read_config(config_path)
    critical = parse_float(config.get("critical_temp_c", "85"), 85.0)
    result: list[str] = []
    if not os.path.isdir("/sys/module/qnap8528"):
        result.append("qnap8528 module missing")
    fault = read_text(FAN_FAULT_FLAG)
    if fault:
        result.append(f"fan fault: {fault}")
    temperature = max_temp()
    if temperature is not None and temperature >= critical:
        result.append(f"temperature {temperature:.0f}C")
    units = failed_units()
    if units:
        result.append("failed units: " + ",".join(units[:4]))
    return result


def apply(state: str) -> bool:
    if state == "critical":
        return set_led("status", 2)
    if state == "off":
        return set_led("status", 0)
    return set_led("status", 1)


def main() -> int:
    parser = argparse.ArgumentParser(description="TVS-1288X status LED policy")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--off", action="store_true")
    args = parser.parse_args()
    if not require_supported_model():
        return 4
    if args.off:
        return 0 if apply("off") else 1
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    last: list[str] | None = None
    while RUNNING:
        config = read_config(args.config)
        interval = max(5.0, parse_float(config.get("check_seconds", "30"), 30.0))
        current = reasons(args.config)
        state = "critical" if current else "ok"
        if current != last:
            print(f"STATUS {state.upper()}: " + (" | ".join(current) if current else "healthy"), flush=True)
            last = current
        if not apply(state):
            print("STATUS ERROR: qnap8528 status LED unavailable", flush=True)
            return 1
        if args.once:
            return 1 if current else 0
        if state == "critical":
            set_led("status", 0)
            time.sleep(min(0.5, interval))
            apply("critical")
            time.sleep(max(0.5, interval - 0.5))
        else:
            time.sleep(interval)
    apply("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
