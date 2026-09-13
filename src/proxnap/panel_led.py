from __future__ import annotations

import argparse
import math
import signal
import time

from .common import (
    parse_float,
    parse_int,
    read_config,
    require_supported_model,
    set_led,
)

DEFAULT_CONFIG = "/etc/proxnap/panel-led.conf"
RUNNING = True


def stop(_signum=None, _frame=None) -> None:
    global RUNNING
    RUNNING = False


def settings(path: str) -> tuple[int, int, float, float, int]:
    raw = read_config(path)
    low = max(0, min(100, parse_int(raw.get("minimum", "5"), 5)))
    high = max(low, min(100, parse_int(raw.get("maximum", "70"), 70)))
    period = max(4.0, parse_float(raw.get("period_seconds", "8"), 8.0))
    step = max(0.1, parse_float(raw.get("step_seconds", "0.2"), 0.2))
    static = max(0, min(100, parse_int(raw.get("static_brightness", "40"), 40)))
    return low, high, period, step, static


def main() -> int:
    parser = argparse.ArgumentParser(description="TVS-1288X front-panel brightness")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--static", action="store_true")
    mode.add_argument("--breathe", action="store_true")
    mode.add_argument("--off", action="store_true")
    args = parser.parse_args()
    if not require_supported_model():
        return 4
    low, high, period, step, static = settings(args.config)
    if args.off:
        return 0 if set_led("panel_brightness", 0) else 1
    if args.static:
        return 0 if set_led("panel_brightness", static) else 1
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    started = time.monotonic()
    while RUNNING:
        phase = ((time.monotonic() - started) % period) / period
        wave = (math.sin(phase * math.tau - math.pi / 2.0) + 1.0) / 2.0
        if not set_led("panel_brightness", round(low + (high - low) * wave)):
            print("PANEL ERROR: qnap8528 panel brightness unavailable", flush=True)
            return 1
        time.sleep(step)
    set_led("panel_brightness", static)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
