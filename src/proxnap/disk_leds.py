#!/usr/bin/env python3
"""
Disk activity/error LED monitor for QNAP TVS-1288X on Proxmox.

Uses qnap8528 LED devices:
- brightness 0: off
- brightness 1: green
- brightness 2: red

Policy:
- SMART failure or critical SMART attributes: red blinking.
- Block I/O activity: green while activity is recent.
- Idle and healthy: off.
"""

import argparse
import math
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass


LED_BASE = "/sys/class/leds"
PANEL_LED = "qnap8528::panel_brightness"
DEFAULT_CONFIG = "/etc/proxnap/disk-leds.conf"

POLL_SECONDS = 0.20
ACTIVITY_HOLD_SECONDS = 0.65
SMART_INTERVAL_SECONDS = 60
RED_BLINK_SECONDS = 0.50

CRITICAL_SMART_ATTRS = {
    "Current_Pending_Sector",
    "Offline_Uncorrectable",
}
REALLOCATED_RED_THRESHOLD = 100


running = True


def stop(_signum=None, _frame=None):
    global running
    running = False


def read_file(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def write_file(path: str, value: str) -> bool:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(value)
        return True
    except OSError as exc:
        print(f"WARN: cannot write {path}: {exc}", file=sys.stderr)
        return False


def led_path(name: str) -> str:
    return os.path.join(LED_BASE, f"qnap8528::{name}")


def set_led(name: str, brightness: int) -> bool:
    path = led_path(name)
    if not os.path.isdir(path):
        return False
    write_file(os.path.join(path, "trigger"), "none")
    return write_file(os.path.join(path, "brightness"), str(brightness))


def set_panel_brightness(value: int):
    path = os.path.join(LED_BASE, PANEL_LED, "brightness")
    if os.path.exists(path):
        write_file(path, str(value))


def resolve_block_dev(dev_spec: str) -> str | None:
    dev_spec = dev_spec.strip()
    if not dev_spec:
        return None

    if dev_spec.startswith("/"):
        resolved = os.path.realpath(dev_spec)
        name = os.path.basename(resolved)
    else:
        name = dev_spec.removeprefix("/dev/")

    sys_block = f"/sys/class/block/{name}"
    if not os.path.exists(sys_block):
        return None

    if os.path.exists(os.path.join(sys_block, "partition")):
        parent = os.path.basename(os.path.dirname(os.path.realpath(sys_block)))
        return parent if parent else None

    return name


def block_stat(dev_spec: str) -> tuple[int, int] | None:
    dev = resolve_block_dev(dev_spec)
    if not dev:
        return None
    raw = read_file(f"/sys/block/{dev}/stat")
    if not raw:
        return None
    fields = raw.split()
    if len(fields) < 8:
        return None
    # completed reads + completed writes is enough for activity detection.
    return int(fields[0]), int(fields[4])


def smart_health(dev_spec: str) -> tuple[bool | None, str]:
    dev = resolve_block_dev(dev_spec)
    if not dev:
        return None, "device missing"

    cmd = ["/usr/sbin/smartctl", "-H", "-A", f"/dev/{dev}"]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=12,
        )
    except Exception as exc:
        return None, f"smartctl unavailable: {exc}"

    out = proc.stdout
    health_bad = False
    health_seen = False
    unavailable = bool(proc.returncode & 0b111) or not out.strip()
    reasons: list[str] = []

    for line in out.splitlines():
        if "SMART overall-health self-assessment test result:" in line:
            health_seen = True
            status = line.split(":", 1)[1].strip()
            if status != "PASSED":
                health_bad = True
                reasons.append(line.strip())
        elif "SMART Health Status:" in line:
            health_seen = True
            status = line.split(":", 1)[1].strip().upper()
            if status not in {"OK", "PASSED"}:
                health_bad = True
                reasons.append(line.strip())

        parts = line.split()
        if len(parts) >= 10 and parts[1] in CRITICAL_SMART_ATTRS:
            raw_value = parts[-1]
            match = re.search(r"\d+", raw_value)
            if match and int(match.group(0)) > 0:
                health_bad = True
                reasons.append(f"{parts[1]}={raw_value}")
        elif len(parts) >= 10 and parts[1] in {"Reallocated_Sector_Ct", "Reallocated_Event_Count"}:
            raw_value = parts[-1]
            match = re.search(r"\d+", raw_value)
            if match and int(match.group(0)) >= REALLOCATED_RED_THRESHOLD:
                health_bad = True
                reasons.append(f"{parts[1]}={raw_value}")

    if proc.returncode & 0b11111000:
        health_bad = True
        reasons.append(f"smartctl exit={proc.returncode}")

    if health_bad:
        return True, "; ".join(reasons)
    if unavailable or not health_seen:
        return None, "SMART unavailable"
    return False, "OK"


@dataclass
class Slot:
    led: str
    dev_spec: str
    check_smart: bool = True
    last_stat: tuple[int, int] | None = None
    last_activity: float = 0.0
    last_smart_check: float = 0.0
    smart_bad: bool | None = None
    smart_reason: str = "unchecked"
    red_on: bool = False
    last_blink: float = 0.0
    current_brightness: int = -1

    def exists(self) -> bool:
        return os.path.isdir(led_path(self.led)) and resolve_block_dev(self.dev_spec) is not None

    def label(self) -> str:
        dev = resolve_block_dev(self.dev_spec) or self.dev_spec
        return f"{self.led}/{dev}"

    def update_activity(self, now: float):
        stat = block_stat(self.dev_spec)
        if stat is None:
            return
        if self.last_stat is not None and stat != self.last_stat:
            self.last_activity = now
        self.last_stat = stat

    def update_smart(self, now: float):
        if not self.check_smart:
            self.smart_bad = False
            self.smart_reason = "disabled"
            return
        if resolve_block_dev(self.dev_spec) is None:
            self.smart_bad = None
            self.smart_reason = "missing"
            return
        if now - self.last_smart_check < SMART_INTERVAL_SECONDS:
            return
        self.last_smart_check = now
        bad, reason = smart_health(self.dev_spec)
        if bad != self.smart_bad or reason != self.smart_reason:
            print(f"{self.label()}: SMART {'BAD' if bad else 'UNKNOWN' if bad is None else 'OK'}: {reason}", flush=True)
        self.smart_bad = bad
        self.smart_reason = reason

    def desired_brightness(self, now: float) -> int:
        if not self.exists():
            return 0
        if self.check_smart and self.smart_bad is None:
            return 0
        if self.smart_bad:
            if now - self.last_blink >= RED_BLINK_SECONDS:
                self.red_on = not self.red_on
                self.last_blink = now
            return 2 if self.red_on else 0
        if now - self.last_activity <= ACTIVITY_HOLD_SECONDS:
            return 1
        return 0

    def apply(self, brightness: int):
        if brightness == self.current_brightness:
            return
        if set_led(self.led, brightness):
            self.current_brightness = brightness


def read_config(path: str) -> tuple[list[str], int | None]:
    global POLL_SECONDS, ACTIVITY_HOLD_SECONDS, SMART_INTERVAL_SECONDS
    maps: list[str] = []
    panel_brightness: int | None = None

    if not path or not os.path.exists(path):
        return maps, panel_brightness

    for line_no, line in enumerate(read_file(path).splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line:
            line = line.split("#", 1)[0].strip()
        if "=" not in line:
            print(f"WARN: ignoring invalid config line {path}:{line_no}", file=sys.stderr)
            continue
        key, value = [part.strip() for part in line.split("=", 1)]
        if key == "panel_brightness":
            try:
                panel_brightness = int(value)
            except ValueError:
                print(f"WARN: invalid panel_brightness at {path}:{line_no}", file=sys.stderr)
            continue
        if key in {"poll_seconds", "activity_hold_seconds", "smart_interval_seconds"}:
            number = float(value)
            if not math.isfinite(number) or number <= 0:
                raise ValueError("invalid disk LED timing")
            if key == "poll_seconds":
                POLL_SECONDS = max(0.1, number)
            elif key == "activity_hold_seconds":
                ACTIVITY_HOLD_SECONDS = number
            else:
                SMART_INTERVAL_SECONDS = max(30.0, number)
            continue
        maps.append(f"{key}={value}")

    return maps, panel_brightness


def parse_map(values: list[str], check_smart: bool) -> list[Slot]:
    slots = []
    for value in values:
        if "=" not in value:
            raise ValueError(f"invalid map {value!r}; expected led=blockdev")
        led, dev_spec = [part.strip() for part in value.split("=", 1)]
        led = led.removeprefix("qnap8528::")
        if not re.fullmatch(r"(?:hdd[1-8]|ssd[1-4]|m2ssd[12])", led) or not dev_spec:
            raise ValueError("unsupported bay mapping")
        if any(slot.led == led for slot in slots):
            raise ValueError("duplicate bay mapping")
        slots.append(Slot(led=led, dev_spec=dev_spec, check_smart=check_smart))
    return slots


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="LED=DEV",
        help="slot-to-block mapping, for example --map hdd8=sdb",
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=f"config file, default {DEFAULT_CONFIG}")
    parser.add_argument("--panel-brightness", type=int, default=25)
    parser.add_argument("--no-smart", action="store_true", help="test mode: ignore SMART and show activity only")
    parser.add_argument("--once", action="store_true", help="run one update and exit")
    args = parser.parse_args()

    config_maps, config_panel_brightness = read_config(args.config)
    maps = config_maps + args.map
    panel_brightness = args.panel_brightness
    if config_panel_brightness is not None:
        panel_brightness = config_panel_brightness

    if not maps:
        print("ERROR: pass at least one --map LED=DEV, example: --map hdd8=sdb", file=sys.stderr)
        return 2

    slots = parse_map(maps, check_smart=not args.no_smart)
    set_panel_brightness(max(0, min(100, panel_brightness)))

    for slot in slots:
        if not os.path.isdir(led_path(slot.led)):
            print(f"WARN: LED not found: {slot.led}", file=sys.stderr)
        if resolve_block_dev(slot.dev_spec) is None:
            print(f"WARN: block device not found: {slot.dev_spec}", file=sys.stderr)
        set_led(slot.led, 0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while running:
        now = time.monotonic()
        for slot in slots:
            slot.update_activity(now)
            slot.update_smart(now)
            slot.apply(slot.desired_brightness(now))
        if args.once:
            break
        time.sleep(POLL_SECONDS)

    for slot in slots:
        set_led(slot.led, 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
