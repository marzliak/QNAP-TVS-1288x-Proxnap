#!/usr/bin/env python3
"""
Front LED policy for QNAP TVS-1288X on Proxmox.

Normal: solid green on qnap8528::status, copy LED off.
Critical: red blink on qnap8528::status.
Warning: qnap8528::status stays green; qnap8528::usb pulses on the copy button.

Critical conditions are intentionally conservative:
- failed systemd units
- degraded/faulted/unavailable ZFS pools
- SMART overall failure, pending sectors, offline uncorrectable sectors,
  or high reallocation counts on configured disks
- high temperature
- root filesystem mounted read-only
- qnap8528 module missing
"""

import argparse
import glob
import json
import os
import re
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass


STATUS_LED = "/sys/class/leds/qnap8528::status"
COPY_LED = "/sys/class/leds/qnap8528::usb"
DISK_CONFIG = "/etc/proxnap/disk-leds.conf"
DEFAULT_CONFIG = "/etc/proxnap/status-led.conf"
UPDATE_RUNNING_FLAG = "/run/proxnap-update-running"
STATUS_STATE_FILE = "/run/proxnap-status.json"

running = True
last_update_check = 0.0
last_update_reasons: list[str] = []
last_buzzer: dict[str, float] = {}
# Remember observed channels for this process; partial disappearance is not cool.
known_temp_paths: set[str] = set()


@dataclass
class Settings:
    enabled: bool = True
    check_seconds: float = 30.0
    update_check_seconds: float = 21600.0
    critical_temp_c: int = 85
    reallocated_red_threshold: int = 100
    buzzer_enabled: bool = False
    buzzer_min_interval_seconds: float = 1800.0
    buzzer_boot_command: str = ""
    buzzer_warning_command: str = ""
    buzzer_critical_command: str = ""


def stop(_signum=None, _frame=None):
    global running
    running = False


def read_file(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def parse_bool(value: str, default: bool) -> bool:
    clean = value.strip().lower()
    if clean in {"1", "true", "yes", "on", "enabled"}:
        return True
    if clean in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def parse_float(value: str, default: float) -> float:
    try:
        return float(value.strip())
    except ValueError:
        return default


def parse_int(value: str, default: int) -> int:
    try:
        return int(value.strip(), 0)
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
    settings = Settings()
    settings.enabled = parse_bool(raw.get("enabled", "true"), True)
    settings.check_seconds = max(5.0, parse_float(raw.get("check_seconds", "30"), 30.0))
    settings.update_check_seconds = max(60.0, parse_float(raw.get("update_check_seconds", "21600"), 21600.0))
    settings.critical_temp_c = parse_int(raw.get("critical_temp_c", "85"), 85)
    settings.reallocated_red_threshold = parse_int(
        raw.get("reallocated_red_threshold", "100"), 100
    )
    settings.buzzer_enabled = parse_bool(raw.get("buzzer_enabled", "false"), False)
    settings.buzzer_min_interval_seconds = max(
        60.0, parse_float(raw.get("buzzer_min_interval_seconds", "1800"), 1800.0)
    )
    settings.buzzer_boot_command = raw.get("buzzer_boot_command", "")
    settings.buzzer_warning_command = raw.get("buzzer_warning_command", "")
    settings.buzzer_critical_command = raw.get("buzzer_critical_command", "")
    return settings


def write_file(path: str, value: str) -> bool:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(value)
        return True
    except OSError:
        return False


def write_status_state(status: str, reasons: list[str], warnings: list[str],
                       unavailable: list[str], check_seconds: float):
    payload = {
        "status": status,
        "reasons": reasons,
        "warnings": warnings,
        "timestamp": int(time.time()),
        "unavailable": unavailable,
        "check_seconds": check_seconds,
    }
    tmp_path = f"{STATUS_STATE_FILE}.tmp"
    if write_file(tmp_path, json.dumps(payload, separators=(",", ":")) + "\n"):
        try:
            os.replace(tmp_path, STATUS_STATE_FILE)
        except OSError:
            pass


def sh(args: list[str], timeout: int = 12) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            args,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip()
    except Exception as exc:
        return 127, str(exc)


def run_command(command: str, timeout: int = 10) -> tuple[int, str]:
    if not command.strip():
        return 0, ""
    try:
        proc = subprocess.run(
            shlex.split(command),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip()
    except Exception as exc:
        return 127, str(exc)


def maybe_buzz(settings: Settings, kind: str, command: str, force: bool = False):
    if not settings.buzzer_enabled or not command.strip():
        return
    now = time.monotonic()
    if not force and now - last_buzzer.get(kind, 0.0) < settings.buzzer_min_interval_seconds:
        return
    rc, out = run_command(command)
    if rc != 0:
        print(f"BUZZER {kind} failed rc={rc}: {out}", flush=True)
    last_buzzer[kind] = now


def set_led(path: str, value: int):
    write_file(os.path.join(path, "trigger"), "none")
    write_file(os.path.join(path, "brightness"), str(value))


def set_status(value: int):
    set_led(STATUS_LED, value)


def set_copy(value: int):
    if os.path.isdir(COPY_LED):
        set_led(COPY_LED, value)


def resolve_block_dev(dev_spec: str) -> str | None:
    if not dev_spec:
        return None
    if dev_spec.startswith("/"):
        resolved = os.path.realpath(dev_spec)
        name = os.path.basename(resolved)
    else:
        name = dev_spec.removeprefix("/dev/")
    if not os.path.exists(f"/sys/class/block/{name}"):
        return None
    if os.path.exists(f"/sys/class/block/{name}/partition"):
        parent = os.path.basename(os.path.dirname(os.path.realpath(f"/sys/class/block/{name}")))
        return parent or None
    return name


def configured_disks(unavailable: list[str]) -> list[str]:
    disks = []
    config = read_file(DISK_CONFIG, "\0")
    if config == "\0":
        unavailable.append("disk configuration unavailable")
        return disks
    for line in config.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = [part.strip() for part in line.split("=", 1)]
        if key in {"panel_brightness", "poll_seconds", "activity_hold_seconds", "smart_interval_seconds"}:
            continue
        dev = resolve_block_dev(value)
        if not dev:
            unavailable.append(f"configured disk unavailable: {key}")
        if dev and dev not in disks:
            disks.append(dev)
    return disks


def smart_bad(dev: str, settings: Settings, unavailable: list[str]) -> str | None:
    rc, out = sh(["/usr/sbin/smartctl", "-H", "-A", f"/dev/{dev}"])
    reasons: list[str] = []
    health_seen = False
    if rc < 0 or rc & 0b111:
        unavailable.append(f"{dev}: SMART command unavailable (exit={rc})")

    for line in out.splitlines():
        if line.startswith("SMART overall-health self-assessment test result:"):
            status = line.split(":", 1)[1].strip().upper()
            health_seen = status in {"PASSED", "FAILED"}
            if status == "FAILED":
                reasons.append(line.strip())
            elif status != "PASSED":
                unavailable.append(f"{dev}: unrecognized SMART health")
        elif line.startswith("SMART Health Status:"):
            status = line.split(":", 1)[1].strip().upper()
            health_seen = status in {"OK", "PASSED", "FAILED"}
            if status == "FAILED":
                reasons.append(line.strip())
            elif status not in {"OK", "PASSED"}:
                unavailable.append(f"{dev}: unrecognized SMART health")

        parts = line.split()
        if (len(parts) >= 2 and parts[1] in {"Current_Pending_Sector", "Offline_Uncorrectable",
                "Reallocated_Sector_Ct", "Reallocated_Event_Count"} and len(parts) < 10):
            unavailable.append(f"{dev}: truncated {parts[1]}")
        if len(parts) >= 10 and parts[1] in {"Current_Pending_Sector", "Offline_Uncorrectable"}:
            match = re.fullmatch(r"\d+", parts[-1])
            if not match:
                unavailable.append(f"{dev}: malformed {parts[1]}")
            if match and int(match.group(0)) > 0:
                reasons.append(f"{parts[1]}={parts[-1]}")
        elif len(parts) >= 10 and parts[1] in {"Reallocated_Sector_Ct", "Reallocated_Event_Count"}:
            match = re.fullmatch(r"\d+", parts[-1])
            if not match:
                unavailable.append(f"{dev}: malformed {parts[1]}")
            if match and int(match.group(0)) >= settings.reallocated_red_threshold:
                reasons.append(f"{parts[1]}={parts[-1]}")

    if rc >= 0 and not rc & 0b111 and rc & 0b1000:
        reasons.append(f"smartctl exit={rc}")
    if not health_seen:
        unavailable.append(f"{dev}: SMART health unavailable")
    return f"{dev}: " + "; ".join(reasons) if reasons else None


def max_temp_c(unavailable: list[str]) -> int | None:
    temps = []
    paths = set()
    for hwmon in glob.glob("/sys/class/hwmon/hwmon*"):
        # Avoid NVMe hwmon polling. The NVMe driver path can issue admin
        # commands to devices, and the reference build has seen controllers hang
        # around nvme_hwmon_read. Other thermal sensors remain active.
        name = read_file(os.path.join(hwmon, "name"))
        if name == "nvme":
            continue
        if not name:
            unavailable.append("temperature source name unavailable")
            continue
        paths.update(glob.glob(os.path.join(hwmon, "temp*_input")))
    known_temp_paths.update(paths)
    for path in sorted(known_temp_paths):
        raw = read_file(path) if path in paths else ""
        if re.fullmatch(r"-?[0-9]+", raw) and 0 < int(raw) < 130000:
            temps.append(int(raw) // 1000)
        else:
            unavailable.append(f"temperature unavailable: {path}")
    if not temps:
        unavailable.append("temperature readings unavailable")
    return max(temps) if temps else None


def failed_units(unavailable: list[str]) -> str | None:
    rc, out = sh(["systemctl", "--failed", "--no-legend", "--plain"], timeout=8)
    if rc != 0:
        unavailable.append(f"systemd failed-unit query unavailable (exit={rc})")
        return None
    lines = [line for line in out.splitlines() if line.strip()]
    return "failed systemd units: " + ", ".join(line.split()[0] for line in lines) if lines else None


def zfs_bad() -> str | None:
    if not os.path.exists("/usr/sbin/zpool"):
        return None
    rc, out = sh(["/usr/sbin/zpool", "status", "-x"], timeout=15)
    if "no pools available" in out.lower():
        return None
    if "all pools are healthy" in out.lower():
        return None
    if rc != 0 or out:
        return "zfs: " + out.splitlines()[0]
    return None


def root_read_only() -> str | None:
    for line in read_file("/proc/mounts").splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[1] == "/":
            opts = parts[3].split(",")
            if "ro" in opts:
                return "root filesystem is read-only"
    return None


def health_reasons(settings: Settings, unavailable: list[str]) -> list[str]:
    reasons = []
    if not os.path.isdir("/sys/module/qnap8528"):
        reasons.append("qnap8528 module missing")
    unit_reason = failed_units(unavailable)
    if unit_reason:
        reasons.append(unit_reason)
    for check in (zfs_bad, root_read_only):
        reason = check()
        if reason:
            reasons.append(reason)
    temp = max_temp_c(unavailable)
    if temp is not None and temp >= settings.critical_temp_c:
        reasons.append(f"temperature {temp}C >= {settings.critical_temp_c}C")
    for dev in configured_disks(unavailable):
        reason = smart_bad(dev, settings, unavailable)
        if reason:
            reasons.append(reason)
    return reasons


def update_reasons(settings: Settings, unavailable: list[str]) -> list[str]:
    global last_update_check, last_update_reasons
    now = time.monotonic()
    reasons = []
    if os.path.exists(UPDATE_RUNNING_FLAG):
        reasons.append("update running")
    if os.path.exists("/var/run/reboot-required"):
        reasons.append("reboot required")

    if last_update_check and 0 <= now - last_update_check < settings.update_check_seconds:
        return reasons + last_update_reasons

    rc, out = sh(["apt", "list", "--upgradable"], timeout=30)
    if rc != 0:
        last_update_check = 0.0
        last_update_reasons = []
        unavailable.append("package update check unavailable")
        return reasons
    upgrades = [
        line
        for line in out.splitlines()
        if line.strip() and not line.startswith("Listing") and "/" in line
    ]
    if upgrades:
        reasons.append(f"{len(upgrades)} package updates")
    security = [line for line in upgrades if "security" in line.lower()]
    if security:
        reasons.append(f"{len(security)} security updates")

    last_update_check = now
    # Only package results are cached. Runtime flags are read every cycle.
    last_update_reasons = [reason for reason in reasons
                           if reason not in {"update running", "reboot required"}]
    return reasons


def status_snapshot(settings: Settings) -> tuple[str, list[str], list[str], list[str]]:
    unavailable: list[str] = []
    reasons = health_reasons(settings, unavailable)
    warnings = [] if reasons else update_reasons(settings, unavailable)
    state = ("critical" if reasons else "unknown" if unavailable else
             "warning" if warnings else "ok")
    return state, reasons, warnings, unavailable


def unknown_status(*, status_writer, copy_writer):
    """No healthy indication on unknown data; effects must be explicit."""
    status_writer(0)
    copy_writer(0)


def green_solid(duration: float):
    end = time.monotonic() + duration
    set_status(1)
    set_copy(0)
    while running and time.monotonic() < end:
        time.sleep(min(1.0, max(0.0, end - time.monotonic())))


def red_blink(duration: float):
    end = time.monotonic() + duration
    set_copy(0)
    while running and time.monotonic() < end:
        set_status(2)
        time.sleep(0.55)
        set_status(0)
        time.sleep(0.55)


def warning_copy_pulse(duration: float, warnings: list[str]):
    end = time.monotonic() + duration
    set_status(1)
    urgent = any("security" in warning or "reboot" in warning or "running" in warning for warning in warnings)
    while running and time.monotonic() < end:
        if urgent:
            for _ in range(2):
                set_copy(1)
                time.sleep(0.18)
                set_copy(0)
                time.sleep(0.18)
            time.sleep(1.5)
        else:
            set_copy(1)
            time.sleep(0.25)
            set_copy(0)
            time.sleep(4.75)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--off", action="store_true")
    args = parser.parse_args()
    settings = settings_from_config(args.config)

    if args.off:
        set_status(0)
        set_copy(0)
        return 0

    if not os.path.isdir(STATUS_LED):
        print(f"LED not found: {STATUS_LED}")
        return 1

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    last_reasons: list[str] | None = None
    last_warnings: list[str] | None = None
    boot_buzzed = False
    while running:
        settings = settings_from_config(args.config)
        if not settings.enabled:
            write_status_state("unknown", [], [], ["status monitoring disabled"], settings.check_seconds)
            set_status(0)
            set_copy(0)
            if args.once:
                return 1
            time.sleep(settings.check_seconds)
            continue

        state, reasons, warnings, unavailable = status_snapshot(settings)
        write_status_state(state, reasons, warnings, unavailable, settings.check_seconds)
        if state == "unknown":
            print("STATUS UNKNOWN: " + " | ".join(unavailable), flush=True)
            unknown_status(status_writer=set_status, copy_writer=set_copy)
            if args.once:
                return 1
            time.sleep(settings.check_seconds)
            continue
        if reasons != last_reasons or warnings != last_warnings:
            if reasons:
                print("STATUS CRITICAL: " + " | ".join(reasons), flush=True)
                maybe_buzz(settings, "critical", settings.buzzer_critical_command, force=True)
            elif warnings:
                print("STATUS WARNING: " + " | ".join(warnings), flush=True)
                maybe_buzz(settings, "warning", settings.buzzer_warning_command)
            else:
                print("STATUS OK", flush=True)
                if not boot_buzzed:
                    maybe_buzz(settings, "boot", settings.buzzer_boot_command, force=True)
                    boot_buzzed = True
            last_reasons = reasons
            last_warnings = warnings

        if args.once:
            set_copy(0)
            set_status(2 if reasons else 1)
            return 1 if reasons else 0

        if reasons:
            red_blink(settings.check_seconds)
        elif warnings:
            warning_copy_pulse(settings.check_seconds, warnings)
        else:
            green_solid(settings.check_seconds)

    set_status(0)
    set_copy(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
