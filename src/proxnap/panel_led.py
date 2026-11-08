#!/usr/bin/env python3
"""
Slow breathing for the QNAP front-panel blue LED via panel_brightness.

This controls the global front-panel PWM exposed by qnap8528, so it also
changes the apparent brightness of bay/status LEDs while they are lit.
"""

import argparse
import math
import os
import signal
import time
from dataclasses import dataclass


LED = "/sys/class/leds/qnap8528::panel_brightness"
DEFAULT_CONFIG = "/etc/proxnap/panel-led.conf"
DEFAULT_RESTORE = 50
CONFIG_RELOAD_SECONDS = 15

running = True


@dataclass
class Settings:
    low: int = 5
    high: int = 100
    period: float = 6.0
    step: float = 0.10
    restore: int = DEFAULT_RESTORE
    off_hours_enabled: bool = False
    off_hours: str = "23:00-08:00"
    stealth_brightness: int = 0


def stop(_signum=None, _frame=None):
    global running
    running = False


def read_file(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return default


def write_file(path: str, value: str) -> bool:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(value)
        return True
    except OSError:
        return False


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def parse_bool(value: str, default: bool = False) -> bool:
    clean = value.strip().lower()
    if clean in {"1", "true", "yes", "on", "enabled"}:
        return True
    if clean in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def parse_int(value: str, default: int) -> int:
    try:
        return int(value.strip())
    except ValueError:
        return default


def parse_float(value: str, default: float) -> float:
    try:
        return float(value.strip())
    except ValueError:
        return default


def read_config(path: str) -> dict[str, str]:
    config: dict[str, str] = {}
    for line in read_file(path).splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, value = [part.strip() for part in line.split("=", 1)]
        config[key] = value
    return config


def settings_from_config(path: str) -> Settings:
    raw = read_config(path)
    for old, new in (("minimum", "panel_min"), ("maximum", "panel_max"),
                     ("period_seconds", "panel_period"), ("step_seconds", "panel_step"),
                     ("static_brightness", "panel_restore")):
        if old in raw and new not in raw:
            raw[new] = raw[old]
    settings = Settings()
    settings.low = clamp(parse_int(raw.get("panel_min", str(settings.low)), settings.low), 0, 100)
    settings.high = clamp(parse_int(raw.get("panel_max", str(settings.high)), settings.high), settings.low, 100)
    settings.period = max(4.0, parse_float(raw.get("panel_period", str(settings.period)), settings.period))
    settings.step = max(0.1, parse_float(raw.get("panel_step", str(settings.step)), settings.step))
    settings.restore = clamp(parse_int(raw.get("panel_restore", str(settings.restore)), settings.restore), 0, 100)
    settings.off_hours_enabled = parse_bool(
        raw.get("off_hours_enabled", str(settings.off_hours_enabled)), settings.off_hours_enabled
    )
    settings.off_hours = raw.get("off_hours", settings.off_hours)
    settings.stealth_brightness = clamp(
        parse_int(raw.get("stealth_panel_brightness", str(settings.stealth_brightness)), settings.stealth_brightness),
        0,
        100,
    )
    return settings


def parse_hhmm(value: str) -> int | None:
    try:
        hour, minute = [int(part) for part in value.strip().split(":", 1)]
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def in_time_range(now_minute: int, spec: str) -> bool:
    for item in spec.split(","):
        if "-" not in item:
            continue
        start_s, end_s = [part.strip() for part in item.split("-", 1)]
        start = parse_hhmm(start_s)
        end = parse_hhmm(end_s)
        if start is None or end is None or start == end:
            continue
        if start < end and start <= now_minute < end:
            return True
        if start > end and (now_minute >= start or now_minute < end):
            return True
    return False


def in_off_hours(settings: Settings) -> bool:
    if not settings.off_hours_enabled:
        return False
    now = time.localtime()
    return in_time_range(now.tm_hour * 60 + now.tm_min, settings.off_hours)


def set_brightness(value: int):
    write_file(os.path.join(LED, "trigger"), "none")
    write_file(os.path.join(LED, "brightness"), str(value))


def settings(path: str) -> tuple[int, int, float, float, int]:
    """Compatibility view of the public panel settings API."""
    value = settings_from_config(path)
    return value.low, value.high, value.period, value.step, value.restore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--min", type=int)
    parser.add_argument("--max", type=int)
    parser.add_argument("--period", type=float)
    parser.add_argument("--step", type=float)
    parser.add_argument("--restore", type=int)
    parser.add_argument("--off", action="store_true")
    parser.add_argument("--static", action="store_true")
    parser.add_argument("--breathe", action="store_true")
    args = parser.parse_args()

    if not os.path.isdir(LED):
        print(f"LED not found: {LED}")
        return 1

    settings = settings_from_config(args.config)
    if args.min is not None:
        settings.low = clamp(args.min, 0, 100)
    if args.max is not None:
        settings.high = clamp(args.max, settings.low, 100)
    if args.period is not None:
        settings.period = max(4.0, args.period)
    if args.step is not None:
        settings.step = max(0.05, args.step)
    if args.restore is not None:
        settings.restore = clamp(args.restore, 0, 100)

    if args.off or args.static:
        set_brightness(settings.restore)
        return 0

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    start = time.monotonic()
    next_config_reload = 0.0
    while running:
        now = time.monotonic()
        if now >= next_config_reload:
            settings = settings_from_config(args.config)
            next_config_reload = now + CONFIG_RELOAD_SECONDS

        if in_off_hours(settings):
            set_brightness(settings.stealth_brightness)
            time.sleep(min(settings.step, 1.0))
            continue

        phase = ((now - start) % settings.period) / settings.period
        wave = (math.sin(phase * math.tau - math.pi / 2) + 1) / 2
        value = round(settings.low + (settings.high - settings.low) * wave)
        set_brightness(value)
        time.sleep(settings.step)

    set_brightness(settings.restore)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
