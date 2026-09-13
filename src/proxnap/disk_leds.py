from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass

from .common import parse_int, read_config, read_text, require_supported_model, set_led

DEFAULT_CONFIG = "/etc/proxnap/disk-leds.conf"
RUNNING = True


def stop(_signum=None, _frame=None) -> None:
    global RUNNING
    RUNNING = False


def resolve_device(spec: str) -> str | None:
    clean = spec.removeprefix("/dev/").strip()
    if not clean or "/" in clean or not re.fullmatch(r"[A-Za-z0-9._-]+", clean):
        return None
    path = f"/sys/class/block/{clean}"
    if not os.path.exists(path):
        return None
    if os.path.exists(os.path.join(path, "partition")):
        return None
    return clean


def block_stat(device: str) -> tuple[int, int] | None:
    fields = read_text(f"/sys/class/block/{device}/stat").split()
    if len(fields) < 8 or not all(value.isdigit() for value in (fields[0], fields[4])):
        return None
    return int(fields[0]), int(fields[4])


def smart_bad(device: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["/usr/sbin/smartctl", "-H", "-A", f"/dev/{device}"],
            check=False, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return True, f"SMART check unavailable: {type(exc).__name__}"
    output = result.stdout
    if "SMART overall-health self-assessment test result: FAILED" in output:
        return True, "SMART overall health failed"
    if re.search(r"(?:Current_Pending_Sector|Offline_Uncorrectable)\s+.*\s[1-9][0-9]*$", output, re.MULTILINE):
        return True, "critical SMART attribute"
    if result.returncode & 0b1000:
        return True, f"smartctl exit {result.returncode}"
    return False, "OK"


@dataclass
class Slot:
    led: str
    device: str
    last_stat: tuple[int, int] | None = None
    active_until: float = 0.0
    smart_bad: bool = False
    next_smart: float = 0.0

    def update(self, now: float, smart_interval: float, activity_hold: float) -> int:
        current = block_stat(self.device)
        if current is not None and self.last_stat is not None and current != self.last_stat:
            self.active_until = now + activity_hold
        self.last_stat = current
        if now >= self.next_smart:
            self.smart_bad, reason = smart_bad(self.device)
            self.next_smart = now + smart_interval
            print(f"{self.led}/{self.device}: SMART {'BAD' if self.smart_bad else 'OK'}: {reason}", flush=True)
        if self.smart_bad:
            return 2 if int(now * 2) % 2 else 0
        return 1 if now < self.active_until else 0


def load_slots(path: str) -> list[Slot]:
    raw = read_config(path)
    slots: list[Slot] = []
    allowed = {*(f"hdd{i}" for i in range(1, 9)), *(f"ssd{i}" for i in range(1, 5)), "m2ssd1", "m2ssd2"}
    for led, spec in raw.items():
        if led not in allowed:
            continue
        device = resolve_device(spec)
        if device:
            slots.append(Slot(led, device))
    return slots


def main() -> int:
    parser = argparse.ArgumentParser(description="TVS-1288X disk LED monitor")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not require_supported_model():
        return 4
    raw = read_config(args.config)
    slots = load_slots(args.config)
    if not slots:
        print("DISK LED ERROR: no valid slot-to-device mappings", flush=True)
        return 2
    smart_interval = max(30, parse_int(raw.get("smart_interval_seconds", "300"), 300))
    poll = max(0.1, float(raw.get("poll_seconds", "0.25")))
    activity_hold = max(0.1, float(raw.get("activity_hold_seconds", "0.75")))
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while RUNNING:
        now = time.monotonic()
        for slot in slots:
            if not set_led(slot.led, slot.update(now, smart_interval, activity_hold)):
                print(f"DISK LED ERROR: LED unavailable: {slot.led}", flush=True)
                return 1
        if args.once:
            break
        time.sleep(poll)
    for slot in slots:
        set_led(slot.led, 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
