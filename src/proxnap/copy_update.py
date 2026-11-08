#!/usr/bin/env python3
"""
Use the QNAP USB Copy button as a guarded Proxmox update trigger.

Hold for check: refresh apt metadata and log current update state.
Multi-click: refresh apt metadata and run the configured upgrade command.
Longer multi-click sequences: arm graceful reboot/poweroff with confirmation.
Manual run: the LCD menu can call this same guarded upgrade path.

The update path intentionally never reboots the machine. Confirmed reboot and
poweroff actions are explicit Copy-button actions with LCD confirmation.
"""

import argparse
import fcntl
import os
import shlex
import signal
import struct
import subprocess
import termios
import time
from dataclasses import dataclass

from proxnap.status_ipc import send_status_notice


DEFAULT_CONFIG = "/etc/proxnap/copy-button.conf"
EVENT = struct.Struct("llHHi")
EV_KEY = 1
BTN_COPY = 0x101
LCD_WIDTH = 16
running = True


@dataclass
class Settings:
    enabled: bool = False
    single_click_status_enabled: bool = False
    status_socket: str = "@proxnap-lcd-status"
    button_input: str = "/dev/input/by-path/platform-qnap8528-event"
    button_code: int = BTN_COPY
    check_hold_seconds: float = 2.0
    upgrade_hold_seconds: float = 0.0
    upgrade_click_count: int = 5
    upgrade_click_window_seconds: float = 5.0
    reboot_click_count: int = 10
    poweroff_click_count: int = 15
    upgrade_click_min: int = 4
    upgrade_click_max: int = 7
    reboot_click_min: int = 8
    poweroff_click_min: int = 13
    click_sequence_window_seconds: float = 8.0
    click_sequence_settle_seconds: float = 2.0
    confirm_click_count: int = 3
    confirm_window_seconds: float = 5.0
    debounce_seconds: float = 0.03
    lock_file: str = "/run/lock/proxnap-action.lock"
    running_flag: str = "/run/proxnap-update-running"
    log_file: str = "/var/log/proxnap-copy-update.log"
    reboot_enabled: bool = False
    poweroff_enabled: bool = False
    lcd_enabled: bool = True
    lcd_port: str = "/dev/ttyS1"
    lcd_message_seconds: float = 8.0
    lcd_off_after_message: bool = False
    lcd_lock_file: str = "/run/proxnap-lcd-message.lock"
    lcd_restore_service: str = "proxnap-lcd.service"
    short_refresh_enabled: bool = False
    upgrade_enabled: bool = False
    refresh_command: str = "apt-get update"
    upgrade_command: str = "apt-get -y -o Dpkg::Options::=--force-confold dist-upgrade"


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
    if value.lower() in {"1", "yes", "true", "on"}:
        return True
    if value.lower() in {"0", "no", "false", "off"}:
        return False
    return default


def parse_float(value: str, default: float) -> float:
    try:
        return float(value)
    except ValueError:
        return default


def parse_int(value: str, default: int) -> int:
    try:
        return int(value, 0)
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


def load_settings(path: str) -> Settings:
    raw = read_config(path)
    settings = Settings()
    settings.enabled = parse_bool(raw.get("enabled", "false"), False)
    settings.single_click_status_enabled = parse_bool(raw.get("single_click_status_enabled", "false"), False)
    settings.status_socket = raw.get("status_socket", settings.status_socket)
    settings.button_input = raw.get("button_input", settings.button_input)
    settings.button_code = parse_int(raw.get("button_code", hex(settings.button_code)), settings.button_code)
    legacy_hold = raw.get("hold_seconds")
    settings.check_hold_seconds = max(1.0, parse_float(raw.get("check_hold_seconds", "2"), 2.0))
    settings.upgrade_hold_seconds = parse_float(raw.get("upgrade_hold_seconds", legacy_hold or "0"), 0.0)
    if settings.upgrade_hold_seconds > 0:
        settings.upgrade_hold_seconds = max(settings.check_hold_seconds + 1.0, settings.upgrade_hold_seconds)
    settings.upgrade_click_count = max(2, parse_int(raw.get("upgrade_click_count", "5"), 5))
    settings.upgrade_click_window_seconds = max(
        1.0,
        parse_float(raw.get("upgrade_click_window_seconds", "5"), 5.0),
    )
    settings.reboot_click_count = max(
        settings.upgrade_click_count + 1,
        parse_int(raw.get("reboot_click_count", "10"), 10),
    )
    settings.poweroff_click_count = max(
        settings.reboot_click_count + 1,
        parse_int(raw.get("poweroff_click_count", "15"), 15),
    )
    settings.upgrade_click_min = max(
        2,
        parse_int(raw.get("upgrade_click_min", "4"), 4),
    )
    settings.upgrade_click_max = max(
        settings.upgrade_click_min,
        parse_int(raw.get("upgrade_click_max", "7"), 7),
    )
    settings.reboot_click_min = max(
        settings.upgrade_click_max + 1,
        parse_int(raw.get("reboot_click_min", "8"), 8),
    )
    settings.poweroff_click_min = max(
        settings.reboot_click_min + 1,
        parse_int(raw.get("poweroff_click_min", "13"), 13),
    )
    default_sequence_window = max(settings.upgrade_click_window_seconds, 8.0)
    settings.click_sequence_window_seconds = max(
        settings.upgrade_click_window_seconds,
        parse_float(
            raw.get("click_sequence_window_seconds", str(default_sequence_window)),
            default_sequence_window,
        ),
    )
    settings.click_sequence_settle_seconds = max(
        0.2,
        parse_float(raw.get("click_sequence_settle_seconds", "2.0"), 2.0),
    )
    settings.confirm_click_count = max(2, parse_int(raw.get("confirm_click_count", "3"), 3))
    settings.confirm_window_seconds = max(
        2.0,
        parse_float(raw.get("confirm_window_seconds", "5"), 5.0),
    )
    settings.debounce_seconds = max(0.0, parse_float(raw.get("debounce_seconds", "0.03"), 0.03))
    settings.lock_file = raw.get("lock_file", settings.lock_file)
    settings.running_flag = raw.get("running_flag", settings.running_flag)
    settings.log_file = raw.get("log_file", settings.log_file)
    settings.lcd_enabled = parse_bool(raw.get("lcd_enabled", "true"), True)
    settings.lcd_port = raw.get("lcd_port", settings.lcd_port)
    settings.lcd_message_seconds = max(1.0, parse_float(raw.get("lcd_message_seconds", "8"), 8.0))
    settings.lcd_off_after_message = parse_bool(raw.get("lcd_off_after_message", "false"), False)
    settings.lcd_lock_file = raw.get("lcd_lock_file", settings.lcd_lock_file)
    settings.lcd_restore_service = raw.get("lcd_restore_service", settings.lcd_restore_service)
    settings.short_refresh_enabled = parse_bool(raw.get("short_refresh_enabled", raw.get("apt_refresh_enabled", "false")), False)
    settings.upgrade_enabled = parse_bool(raw.get("upgrade_enabled", raw.get("apt_upgrade_enabled", "false")), False)
    settings.refresh_command = raw.get("refresh_command", settings.refresh_command)
    settings.upgrade_command = raw.get("upgrade_command", settings.upgrade_command)
    settings.reboot_enabled = parse_bool(raw.get("reboot_enabled", "false"), False)
    settings.poweroff_enabled = parse_bool(raw.get("poweroff_enabled", "false"), False)
    return settings


def log(settings: Settings, message: str):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + message
    print(line, flush=True)
    try:
        with open(settings.log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def fit_lcd(text: str) -> bytes:
    return f"{text:<{LCD_WIDTH}.{LCD_WIDTH}s}".encode("ascii", "replace")


def action_prefix(action: str) -> str:
    return {
        "upgrade": "5.1.1",
        "update": "5.1.1",
        "refreshrun": "5.1.1",
        "check": "5.1.2",
        "run": "5.1.3",
        "reboot": "5.2",
        "poweroff": "5.3",
        "shutdown": "5.3",
    }.get(action, "5.x")


def lcd_action(settings: Settings, action: str, title: str, detail: str, final: bool = False):
    lcd_message(settings, f"{action_prefix(action)} {title}", detail, final=final)


class Lcd:
    def __init__(self, port: str):
        self.port = port
        self.fd: int | None = None

    def open(self):
        self.fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        attrs = termios.tcgetattr(self.fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = 0
        attrs[4] = termios.B1200
        attrs[5] = termios.B1200
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def write(self, data: bytes, delay: float = 0.08):
        if self.fd is None:
            return
        os.write(self.fd, data)
        time.sleep(delay)

    def clear(self):
        self.write(b"\x4d\x28", 0.15)
        self.write_lines("", "")

    def backlight(self, on: bool):
        self.write(b"\x4d\x5e" + bytes([1 if on else 0]), 0.05)

    def stop_clock(self):
        self.write(b"\x4d\x0d", 0.05)

    def write_lines(self, line1: str, line2: str):
        self.write(b"\x4d\x0c\x00\x10" + fit_lcd(line1))
        self.write(b"\x4d\x0c\x01\x10" + fit_lcd(line2))


def lcd_message(settings: Settings, line1: str, line2: str, final: bool = False):
    if not settings.lcd_enabled:
        return
    try:
        os.makedirs(os.path.dirname(settings.lcd_lock_file), exist_ok=True)
        with open(settings.lcd_lock_file, "w", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            lcd = Lcd(settings.lcd_port)
            lcd.open()
            try:
                lcd.stop_clock()
                lcd.backlight(True)
                lcd.write_lines(line1, line2)
                if final:
                    time.sleep(settings.lcd_message_seconds)
                    if settings.lcd_off_after_message:
                        lcd.clear()
                        lcd.backlight(False)
            finally:
                lcd.close()
    except OSError as exc:
        log(settings, f"lcd message failed: {exc}")


def restart_service(name: str):
    if not name:
        return
    try:
        subprocess.run(
            ["systemctl", "try-restart", name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception:
        pass


def restore_lcd(settings: Settings):
    restart_service(settings.lcd_restore_service)


def refresh_status_led():
    restart_service("proxnap-status-led.service")


def run_capture(args: list[str], timeout: int = 60) -> tuple[int, str]:
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


def update_summary() -> str:
    rc, out = run_capture(["apt", "list", "--upgradable"], timeout=30)
    if rc != 0:
        return "apt update list unavailable"
    updates = [
        line
        for line in out.splitlines()
        if line.strip() and not line.startswith("Listing") and "/" in line
    ]
    security = [line for line in updates if "security" in line.lower()]
    reboot = os.path.exists("/var/run/reboot-required")
    parts = [f"{len(updates)} updates"]
    if security:
        parts.append(f"{len(security)} security")
    if reboot:
        parts.append("reboot required")
    return ", ".join(parts)


def update_counts() -> tuple[int, int, bool] | None:
    rc, out = run_capture(["apt", "list", "--upgradable"], timeout=30)
    if rc != 0:
        return None
    updates = [
        line
        for line in out.splitlines()
        if line.strip() and not line.startswith("Listing") and "/" in line
    ]
    security = [line for line in updates if "security" in line.lower()]
    return len(updates), len(security), os.path.exists("/var/run/reboot-required")


def show_update_result(settings: Settings, action: str):
    counts = update_counts()
    if counts is None:
        lcd_action(settings, "update", "Update error", "See log", final=True)
        return
    updates, security, reboot = counts
    if action in {"upgrade", "run"}:
        if reboot:
            lcd_action(settings, "reboot", "Need reboot", "10 clicks", final=True)
        elif updates:
            lcd_action(settings, action, "Done", f"{updates} left", final=True)
        else:
            lcd_action(settings, action, "Done", "0 pending", final=True)
        return
    if updates == 0:
        lcd_action(settings, "check", "Updates", "0 pending", final=True)
        return
    suffix = f" {security}sec" if security else ""
    lcd_action(settings, "check", "Updates", f"{updates} pkg{suffix}", final=True)


def run_system_action(settings: Settings, action: str):
    normalized = "poweroff" if action == "shutdown" else action
    if (normalized == "reboot" and not settings.reboot_enabled) or (normalized == "poweroff" and not settings.poweroff_enabled):
        log(settings, "action disabled by config")
        return

    with open(settings.lock_file, "w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log(settings, "system action ignored: update/action already running")
            lcd_message(settings, "Action blocked", "update running", final=True)
            return

        if action == "reboot":
            log(settings, "copy action confirmed: reboot")
            lcd_action(settings, "reboot", "Rebooting", "Please wait")
            subprocess.Popen(["systemctl", "reboot"])
        elif action in {"poweroff", "shutdown"}:
            log(settings, "copy action confirmed: poweroff")
            lcd_action(settings, "poweroff", "Powering off", "Please wait")
            subprocess.Popen(["systemctl", "poweroff"])
        else:
            log(settings, f"unknown system action ignored: {action}")


def run_stream(settings: Settings, command: str) -> int:
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    args = shlex.split(command)
    log(settings, f"RUN {' '.join(args)}")
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        log(settings, line.rstrip())
    rc = proc.wait()
    log(settings, f"EXIT {rc}: {' '.join(args)}")
    return rc


def finish_update_action(settings: Settings, clear_running: bool = False):
    """Best-effort recovery must not replace the original command/UI error."""
    def report(exc):
        try:
            log(settings, f"update cleanup failed: {exc}")
        except Exception:
            pass

    if clear_running:
        try:
            os.unlink(settings.running_flag)
        except FileNotFoundError:
            pass
        except Exception as exc:
            report(exc)
    for cleanup in (refresh_status_led, lambda: restore_lcd(settings)):
        try:
            cleanup()
        except Exception as exc:
            report(exc)


def perform_upgrade(settings: Settings):
    if not settings.upgrade_enabled or not settings.short_refresh_enabled:
        try:
            log(settings, "upgrade disabled by config")
            lcd_action(settings, "upgrade", "Update off", "Config", final=True)
        finally:
            finish_update_action(settings)
        return

    with open(settings.lock_file, "w", encoding="utf-8") as lock:
        clear_running = False
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log(settings, "upgrade already running")
                lcd_action(settings, "upgrade", "Running", "Please wait", final=True)
                return
            os.makedirs(os.path.dirname(settings.running_flag), exist_ok=True)
            clear_running = True  # Includes partial flag creation/write failures.
            with open(settings.running_flag, "w", encoding="utf-8") as flag:
                flag.write(str(os.getpid()))
            log(settings, "copy button confirmed update")
            lcd_action(settings, "upgrade", "Updating", "Do not power off")
            log(settings, "before: " + update_summary())
            rc = run_stream(settings, settings.refresh_command)
            if rc == 0:
                lcd_action(settings, "upgrade", "Installing", "Please wait")
                rc = run_stream(settings, settings.upgrade_command)
            if rc != 0:
                log(settings, f"update command failed: {rc}")
                lcd_action(settings, "upgrade", "Apt error", "See log", final=True)
                return
            log(settings, "after: " + update_summary())
            refresh_status_led()
            show_update_result(settings, "upgrade")
        finally:
            finish_update_action(settings, clear_running)


def perform_check(settings: Settings, refresh: bool = True):
    if refresh and not settings.short_refresh_enabled:
        try:
            log(settings, "package refresh disabled by config")
        finally:
            finish_update_action(settings)
        return
    with open(settings.lock_file, "w", encoding="utf-8") as lock:
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log(settings, "check ignored: update/action already running")
                lcd_action(settings, "check", "Running", "Please wait", final=True)
                return
            log(settings, "manual update refresh")
            lcd_action(settings, "check", "Checking", "Please wait")
            rc = run_stream(settings, settings.refresh_command) if refresh else 0
            if rc != 0:
                log(settings, f"check command failed: {rc}")
                lcd_action(settings, "check", "Apt error", "See log", final=True)
            else:
                refresh_status_led()
                log(settings, "check: " + update_summary())
                show_update_result(settings, "check")
        finally:
            finish_update_action(settings)


def perform_install_only(settings: Settings):
    if not settings.upgrade_enabled:
        try:
            log(settings, "install-only disabled by config")
            lcd_action(settings, "run", "Update off", "Config", final=True)
        finally:
            finish_update_action(settings)
        return

    with open(settings.lock_file, "w", encoding="utf-8") as lock:
        clear_running = False
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log(settings, "install-only already running")
                lcd_action(settings, "run", "Running", "Please wait", final=True)
                return
            os.makedirs(os.path.dirname(settings.running_flag), exist_ok=True)
            clear_running = True
            with open(settings.running_flag, "w", encoding="utf-8") as flag:
                flag.write(str(os.getpid()))
            log(settings, "manual install-only update")
            lcd_action(settings, "run", "Installing", "Please wait")
            rc = run_stream(settings, settings.upgrade_command)
            if rc != 0:
                log(settings, f"install-only command failed: {rc}")
                lcd_action(settings, "run", "Apt error", "See log", final=True)
                return
            log(settings, "after install-only: " + update_summary())
            refresh_status_led()
            show_update_result(settings, "run")
        finally:
            finish_update_action(settings, clear_running)


def open_input(path: str) -> int | None:
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        # EVIOCSCLOCKID: compare capture times with monotonic prompt/deadline
        # barriers, not with the time a queued event happens to be processed.
        fcntl.ioctl(fd, 0x400445A0, struct.pack("i", time.CLOCK_MONOTONIC))
        return fd
    except OSError:
        if fd is not None:
            os.close(fd)
        return None


def loop(settings: Settings) -> int:
    press_started: float | None = None
    click_times: list[float] = []
    confirm_times: list[float] = []
    pending_action: str | None = None
    pending_until = 0.0
    last_release: float | None = None
    last_event: float | None = None
    accept_after = time.monotonic()
    discard_until_release = False
    syncing = False
    single_status_eligible = True
    upgrade_hold = (
        f", {settings.upgrade_hold_seconds:.0f}s to update"
        if settings.upgrade_hold_seconds > 0
        else ""
    )
    log(
        settings,
        f"listening on {settings.button_input} code 0x{settings.button_code:x}; "
        f"hold {settings.check_hold_seconds:.0f}s to check, "
        f"{settings.upgrade_click_min}-{settings.upgrade_click_max} clicks to arm update, "
        f"{settings.reboot_click_min}-{settings.poweroff_click_min - 1} clicks to arm reboot, "
        f"{settings.poweroff_click_min}+ clicks to arm poweroff{upgrade_hold}",
    )

    def reset_state():
        nonlocal press_started, pending_action, pending_until, last_release, last_event
        nonlocal accept_after, discard_until_release, syncing, single_status_eligible
        press_started = None
        click_times.clear()
        confirm_times.clear()
        pending_action = None
        pending_until = 0.0
        last_release = None
        last_event = None
        accept_after = time.monotonic()
        discard_until_release = False
        syncing = False
        single_status_eligible = True

    def arm_action(action: str):
        nonlocal confirm_times, pending_action, pending_until, accept_after
        pending_action = action
        confirm_times = []
        if action == "upgrade":
            log(settings, "copy sequence armed: update")
            lcd_action(settings, "update", "Confirm", f"{settings.confirm_click_count} clicks")
        elif action == "reboot":
            log(settings, "copy sequence armed: reboot")
            lcd_action(settings, "reboot", "Confirm", f"{settings.confirm_click_count} clicks")
        else:
            log(settings, "copy sequence armed: poweroff")
            lcd_action(settings, "poweroff", "Confirm", f"{settings.confirm_click_count} clicks")
        # A distinct press must be captured AFTER the prompt finishes writing.
        accept_after = time.monotonic()
        pending_until = accept_after + settings.confirm_window_seconds

    def settle_sequence(now: float):
        nonlocal click_times, single_status_eligible
        if not click_times:
            return
        click_times = [t for t in click_times if now - t <= settings.click_sequence_window_seconds]
        if not click_times:
            return
        if now - click_times[-1] < settings.click_sequence_settle_seconds:
            return
        count = len(click_times)
        click_times = []
        status_eligible = single_status_eligible
        single_status_eligible = True
        if count == 1 and status_eligible and settings.single_click_status_enabled and not pending_action:
            sent = send_status_notice(settings.status_socket, "status")
            log(settings, "copy status request " + ("sent" if sent else "unavailable"))
        elif count >= settings.poweroff_click_min:
            arm_action("poweroff")
        elif count >= settings.reboot_click_min:
            arm_action("reboot")
        elif settings.upgrade_click_min <= count <= settings.upgrade_click_max:
            arm_action("upgrade")
        else:
            log(settings, f"copy click sequence ignored: {count} clicks")

    def cancel_pending(now: float):
        if pending_action and now > pending_until:
            action = pending_action
            reset_state()
            log(settings, f"copy confirmation timed out: {action}")
            lcd_action(settings, action, "Cancelled", "Copy button", final=True)
            restore_lcd(settings)
            reset_state()
            return True
        return False

    def confirm_or_count_click(now: float):
        nonlocal click_times, confirm_times, pending_action, accept_after, single_status_eligible
        if pending_action:
            confirm_times.append(now)
            remaining = settings.confirm_click_count - len(confirm_times)
            log(
                settings,
                f"copy confirm {pending_action}: {len(confirm_times)}/{settings.confirm_click_count}",
            )
            if remaining > 0:
                lcd_action(settings, pending_action, "Confirm", f"{remaining} more")
                accept_after = time.monotonic()
            else:
                action = pending_action
                reset_state()
                if action == "upgrade":
                    perform_upgrade(settings)
                else:
                    run_system_action(settings, action)
                reset_state()
            return

        click_times.append(now)
        if len(click_times) > 1:
            single_status_eligible = False
        click_times = [
            t for t in click_times if now - t <= settings.click_sequence_window_seconds
        ]
        log(settings, f"copy click sequence: {len(click_times)} clicks")

    while running:
        # A reopened device never inherits presses, fragments or confirmations.
        reset_state()
        fd = open_input(settings.button_input)
        if fd is None:
            log(settings, f"input not found or clock unavailable: {settings.button_input}")
            time.sleep(5)
            continue
        reset_state()
        buffered = b""
        try:
            while running:
                try:
                    data = os.read(fd, EVENT.size * 16)
                except BlockingIOError:
                    now = time.monotonic()
                    if not cancel_pending(now) and press_started is None and not buffered and not syncing:
                        # Settle only after draining the queue, never between batches.
                        settle_sequence(now)
                    time.sleep(0.1)
                    continue
                except OSError:
                    break
                if not data:  # EOF is a disconnect, not an empty polling batch.
                    break
                buffered += data
                complete = len(buffered) - len(buffered) % EVENT.size
                batch, buffered = buffered[:complete], buffered[complete:]
                for offset in range(0, len(batch), EVENT.size):
                    sec, usec, event_type, code, value = EVENT.unpack_from(batch, offset)
                    if event_type == 0 and code == 3:  # EV_SYN / SYN_DROPPED
                        reset_state()
                        syncing = True
                        continue
                    if syncing:
                        if event_type == 0 and code == 0:  # SYN_REPORT
                            reset_state()
                        continue
                    if event_type != EV_KEY or code != settings.button_code:
                        continue
                    now = time.monotonic()
                    captured = sec + usec / 1_000_000
                    if cancel_pending(now):
                        continue
                    if (not 0 <= usec < 1_000_000 or captured <= accept_after
                            or captured > now or (last_event is not None and captured < last_event)):
                        reset_state()
                        continue
                    last_event = captured
                    if value == 2:  # Autorepeat is not a click or a new press.
                        single_status_eligible = False
                        continue
                    if discard_until_release:
                        if value == 0:
                            discard_until_release = False
                            last_release = captured
                        continue
                    if value == 1:
                        if (press_started is not None or (last_release is not None
                                and captured - last_release < settings.debounce_seconds)):
                            reset_state()
                            discard_until_release = True
                            continue
                        press_started = captured
                        if settings.single_click_status_enabled:
                            send_status_notice(settings.status_socket, "cancel")
                        log(settings, "copy button down")
                    elif value == 0 and press_started is not None:
                        held = captured - press_started
                        press_started = None
                        last_release = captured
                        if held < settings.debounce_seconds:
                            reset_state()
                            continue
                        if held >= settings.check_hold_seconds:
                            # A hold always cancels a pending click confirmation.
                            # Legacy direct-upgrade hold remains explicit opt-in (default 0).
                            reset_state()
                            if settings.upgrade_hold_seconds > 0 and held >= settings.upgrade_hold_seconds:
                                perform_upgrade(settings)
                            else:
                                log(settings, f"copy button check hold ({held:.1f}s)")
                                perform_check(settings, refresh=settings.short_refresh_enabled)
                            reset_state()
                        else:
                            confirm_or_count_click(captured)
                    elif value != 0:
                        # Unknown key values invalidate the whole gesture.
                        reset_state()
                        discard_until_release = True
        finally:
            os.close(fd)
            reset_state()
        # Bound repeated EOF/error reopen attempts just like missing input.
        if running:
            time.sleep(1)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--reboot", action="store_true")
    parser.add_argument("--poweroff", action="store_true")
    parser.add_argument("--check-updates", action="store_true")
    parser.add_argument("--run-install", action="store_true")
    parser.add_argument("--run-upgrade", action="store_true")
    args = parser.parse_args()

    settings = load_settings(args.config)
    if args.reboot or args.poweroff:
        action = "reboot" if args.reboot else "poweroff"
        allowed = settings.reboot_enabled if args.reboot else settings.poweroff_enabled
        if not allowed:
            return 3
        run_system_action(settings, action)
        return 0
    if args.summary:
        print(update_summary())
        return 0
    if args.check_updates:
        if not settings.short_refresh_enabled:
            return 3
        perform_check(settings)
        return 0
    if args.run_install:
        if not settings.upgrade_enabled:
            return 3
        perform_install_only(settings)
        return 0
    if args.run_upgrade:
        if not settings.upgrade_enabled or not settings.short_refresh_enabled:
            return 3
        perform_upgrade(settings)
        return 0
    if not settings.enabled:
        log(settings, "disabled by config")
        return 0

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return loop(settings)


if __name__ == "__main__":
    raise SystemExit(main())
