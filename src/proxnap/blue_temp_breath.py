#!/usr/bin/env python3
"""
Temperature-aware pulse for the QNAP blue front-panel LED.

The qnap8528 USB LED is binary (0/1), so this approximates a breathing LED
with short pulses. It does not touch panel_brightness, avoiding interference
with disk LEDs.
"""

import argparse
import glob
import math
import os
import re
import signal
import time


LED_BASE = "/sys/class/leds"
DEFAULT_LED = "qnap8528::usb"

running = True


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


def find_hwmon(name: str) -> str | None:
    for path in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        if read_file(os.path.join(path, "name")) == name:
            return path
    return None


def read_cpu_temp_c() -> int | None:
    coretemp = find_hwmon("coretemp")
    if coretemp:
        temps = []
        for path in glob.glob(os.path.join(coretemp, "temp*_input")):
            raw = read_file(path)
            if re.fullmatch(r"[0-9]+", raw) and 0 < int(raw) < 130000:
                temps.append(int(raw) // 1000)
        if temps:
            return max(temps)

    qnap = find_hwmon("qnap8528")
    if qnap:
        raw = read_file(os.path.join(qnap, "temp1_input"))
        if re.fullmatch(r"[0-9]+", raw) and 0 < int(raw) < 130000:
            return int(raw) // 1000

    return None


def cycle_seconds(temp_c: int) -> float:
    if temp_c >= 75:
        return 1.2
    if temp_c >= 65:
        return 1.8
    if temp_c >= 55:
        return 2.8
    return 4.0


def pulse_width_seconds(temp_c: int, phase: float) -> float:
    # A binary LED cannot fade, so pulse width carries the "breath" shape.
    wave = (math.sin(phase * math.tau - math.pi / 2) + 1) / 2
    if temp_c >= 75:
        return 0.08 + 0.42 * wave
    if temp_c >= 65:
        return 0.05 + 0.28 * wave
    if temp_c >= 55:
        return 0.04 + 0.18 * wave
    return 0.03 + 0.12 * wave


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--led", default=DEFAULT_LED)
    parser.add_argument("--off", action="store_true", help="turn LED off and exit")
    args = parser.parse_args()

    led_path = os.path.join(LED_BASE, args.led)
    brightness = os.path.join(led_path, "brightness")
    trigger = os.path.join(led_path, "trigger")

    if not os.path.isdir(led_path):
        print(f"LED not found: {args.led}")
        return 1

    write_file(trigger, "none")
    if args.off:
        write_file(brightness, "0")
        return 0

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while running:
        temp_c = read_cpu_temp_c()
        if temp_c is None:
            write_file(brightness, "0")
            time.sleep(4.0)
            continue
        cycle = cycle_seconds(temp_c)
        steps = 12

        for step in range(steps):
            if not running:
                break
            phase = step / steps
            width = min(pulse_width_seconds(temp_c, phase), cycle / steps * 0.85)
            slot = cycle / steps
            write_file(brightness, "1")
            time.sleep(width)
            write_file(brightness, "0")
            time.sleep(max(0.02, slot - width))

    write_file(brightness, "0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
