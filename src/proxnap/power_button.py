#!/usr/bin/env python3
"""
Guard the physical power button behind deliberate click sequences.

On this chassis the ACPI power button reports instant press/release events even
when physically held. We therefore avoid hold-duration policies and use
multi-click sequences with a second confirmation sequence.
"""

import argparse
import fcntl
import os
import selectors
import signal
import struct
import subprocess
import termios
import time
from dataclasses import dataclass


DEFAULT_CONFIG = "/etc/proxnap/power-button.conf"
EVENT = struct.Struct("llHHi")
EV_KEY = 1
KEY_POWER = 116
LCD_WIDTH = 16
running = True


@dataclass
class Settings:
    enabled: bool = False
    input_paths: list[str] | None = None
    reboot_click_count: int = 5
    poweroff_click_count: int = 10
    sequence_window_seconds: float = 3.0
    sequence_settle_seconds: float = 0.6
    confirm_click_count: int = 3
    confirm_window_seconds: float = 3.0
    debounce_seconds: float = 0.15
    lock_file: str = "/run/lock/proxnap-action.lock"
    log_file: str = "/var/log/proxnap-power-button.log"
    reboot_enabled: bool = False
    poweroff_enabled: bool = False
    lcd_enabled: bool = True
    lcd_port: str = "/dev/ttyS1"
    lcd_lock_file: str = "/run/proxnap-lcd-message.lock"

    def paths(self) -> list[str]:
        if self.input_paths:
            return self.input_paths
        return [
            "/dev/input/by-path/platform-PNP0C0C:00-event",
            "/dev/input/by-path/platform-LNXPWRBN:00-event",
        ]


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
    settings.input_paths = [
        item.strip()
        for item in raw.get("input_paths", ",".join(settings.paths())).split(",")
        if item.strip()
    ]
    settings.reboot_click_count = max(2, parse_int(raw.get("reboot_click_count", "5"), 5))
    settings.poweroff_click_count = max(
        settings.reboot_click_count + 1,
        parse_int(raw.get("poweroff_click_count", "10"), 10),
    )
    settings.sequence_window_seconds = max(
        1.0,
        parse_float(raw.get("sequence_window_seconds", "3"), 3.0),
    )
    settings.sequence_settle_seconds = max(
        0.2,
        parse_float(raw.get("sequence_settle_seconds", "0.6"), 0.6),
    )
    settings.confirm_click_count = max(2, parse_int(raw.get("confirm_click_count", "3"), 3))
    settings.confirm_window_seconds = max(
        2.0,
        parse_float(raw.get("confirm_window_seconds", "3"), 3.0),
    )
    settings.debounce_seconds = max(0.0, parse_float(raw.get("debounce_seconds", "0.15"), 0.15))
    settings.lock_file = raw.get("lock_file", settings.lock_file)
    settings.log_file = raw.get("log_file", settings.log_file)
    settings.lcd_enabled = parse_bool(raw.get("lcd_enabled", "true"), True)
    settings.lcd_port = raw.get("lcd_port", settings.lcd_port)
    settings.lcd_lock_file = raw.get("lcd_lock_file", settings.lcd_lock_file)
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

    def backlight(self, on: bool):
        self.write(b"\x4d\x5e" + bytes([1 if on else 0]), 0.05)

    def stop_clock(self):
        self.write(b"\x4d\x0d", 0.05)

    def write_lines(self, line1: str, line2: str):
        self.write(b"\x4d\x0c\x00\x10" + fit_lcd(line1))
        self.write(b"\x4d\x0c\x01\x10" + fit_lcd(line2))


def lcd_message(settings: Settings, line1: str, line2: str):
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
            finally:
                lcd.close()
    except OSError as exc:
        log(settings, f"lcd message failed: {exc}")


def open_input(path: str) -> int | None:
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        # EVIOCSCLOCKID: capture times and feedback barriers must share a clock.
        fcntl.ioctl(fd, 0x400445A0, struct.pack("i", time.CLOCK_MONOTONIC))
        return fd
    except OSError:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        return None


def run_action(settings: Settings, action: str | None = None):
    action = action or "reboot"
    normalized = "poweroff" if action == "shutdown" else action
    if (normalized == "reboot" and not settings.reboot_enabled) or (normalized == "poweroff" and not settings.poweroff_enabled):
        log(settings, "action disabled by config")
        return

    with open(settings.lock_file, "w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log(settings, "action already running")
            return

        if action == "reboot":
            log(settings, "power action confirmed: reboot")
            lcd_message(settings, "Rebooting", "please wait")
            subprocess.Popen(["systemctl", "reboot"])
        elif action in {"poweroff", "shutdown"}:
            log(settings, "power action confirmed: poweroff")
            lcd_message(settings, "Powering off", "please wait")
            subprocess.Popen(["systemctl", "poweroff"])
        else:
            log(settings, f"unknown action ignored: {action}")


def register_inputs(settings: Settings, selector: selectors.DefaultSelector) -> dict[int, str]:
    paths_by_fd: dict[int, str] = {}
    identities = set()
    seen_paths = set()
    fd = None
    try:
        for path in settings.paths():
            if path in seen_paths:
                continue
            seen_paths.add(path)
            fd = open_input(path)
            if fd is None:
                log(settings, f"input not found or clock unavailable: {path}")
                continue
            info = os.fstat(fd)
            identity = (info.st_rdev,) if info.st_rdev else (info.st_dev, info.st_ino)
            if identity in identities:
                duplicate_fd, fd = fd, None
                os.close(duplicate_fd)
                continue
            selector.register(fd, selectors.EVENT_READ)
            paths_by_fd[fd] = path
            identities.add(identity)
            fd = None  # Ownership has transferred to paths_by_fd.
            log(settings, f"listening on {path}")
        return paths_by_fd
    except Exception:
        # Registration may fail halfway through: the caller has no returned map.
        for owned in list(paths_by_fd) + ([] if fd is None else [fd]):
            try:
                selector.unregister(owned)
            except Exception:
                pass
            try:
                os.close(owned)
            except OSError:
                pass
        raise


def loop(settings: Settings) -> int:
    click_times: list[float] = []
    confirm_times: list[float] = []
    pending_action: str | None = None
    ready_action: str | None = None
    pending_until = 0.0
    last_click: float | None = None
    source_fd: int | None = None
    accept_after = time.monotonic()
    presses: dict[int, float | None] = {}

    def reset_state():
        nonlocal pending_action, pending_until, last_click, source_fd, accept_after
        nonlocal ready_action
        click_times.clear()
        confirm_times.clear()
        pending_action = None
        ready_action = None
        pending_until = 0.0
        last_click = None
        source_fd = None
        accept_after = time.monotonic()
        for fd in presses:
            presses[fd] = None

    def arm_action(action: str):
        nonlocal pending_action, pending_until, accept_after
        pending_action = action
        confirm_times.clear()
        if action == "reboot":
            log(settings, "power sequence armed: reboot")
            lcd_message(settings, "Confirm reboot", f"{settings.confirm_click_count} clicks now")
        else:
            log(settings, "power sequence armed: poweroff")
            lcd_message(settings, "Confirm off", f"{settings.confirm_click_count} clicks now")
        # The first confirmation press must be CAPTURED after feedback finishes.
        accept_after = time.monotonic()
        pending_until = accept_after + settings.confirm_window_seconds

    def settle_sequence(now: float):
        nonlocal click_times
        if not click_times:
            return
        click_times = [t for t in click_times if now - t <= settings.sequence_window_seconds]
        if not click_times:
            reset_state()
            return
        if now - click_times[-1] < settings.sequence_settle_seconds:
            return
        count = len(click_times)
        click_times = []
        if count >= settings.poweroff_click_count:
            arm_action("poweroff")
        elif count >= settings.reboot_click_count:
            arm_action("reboot")
        else:
            log(settings, f"power click sequence ignored: {count} clicks")
            reset_state()

    def cancel_pending(now: float):
        if pending_action and now > pending_until:
            log(settings, f"power confirmation timed out: {pending_action}")
            reset_state()
            lcd_message(settings, "Action cancelled", "power button")
            reset_state()
            return True
        return False

    def count_click(captured: float):
        nonlocal click_times, pending_action, accept_after, ready_action
        if pending_action:
            confirm_times.append(captured)
            remaining = settings.confirm_click_count - len(confirm_times)
            log(settings, f"power confirm {pending_action}: {len(confirm_times)}/{settings.confirm_click_count}")
            if remaining > 0:
                lcd_message(settings, f"Confirm {pending_action}", f"{remaining} clicks more")
                accept_after = time.monotonic()
            else:
                action = pending_action
                reset_state()
                # Validate the remainder of ALL drained FD batches first.
                ready_action = action
            return
        click_times.append(captured)
        click_times = [t for t in click_times if captured - t <= settings.sequence_window_seconds]
        log(settings, f"power click sequence: {len(click_times)} clicks")

    while running:
        reset_state()
        selector = selectors.DefaultSelector()
        paths_by_fd: dict[int, str] = {}
        try:
            paths_by_fd = register_inputs(settings, selector)
            if not paths_by_fd:
                time.sleep(5)
                continue
            presses = {fd: None for fd in paths_by_fd}
            buffered = {fd: b"" for fd in paths_by_fd}
            last_event = {fd: None for fd in paths_by_fd}
            syncing = {fd: False for fd in paths_by_fd}
            reset_state()
            while running:
                selector.select(timeout=0.2)
                events = []
                # Drain EVERY registered FD, not just the readiness snapshot.
                # Do not arm between batches or while any record is incomplete.
                for fd in paths_by_fd:
                    for _ in range(64):
                        try:
                            data = os.read(fd, EVENT.size * 16)
                        except BlockingIOError:
                            break
                        if not data:
                            raise OSError("power input EOF")
                        buffered[fd] += data
                        complete = len(buffered[fd]) - len(buffered[fd]) % EVENT.size
                        batch, buffered[fd] = buffered[fd][:complete], buffered[fd][complete:]
                        for offset in range(0, len(batch), EVENT.size):
                            events.append((fd, EVENT.unpack_from(batch, offset)))
                    else:
                        # A continuously noisy device cannot hold an armed action.
                        raise OSError("power input did not drain")
                # Preserve per-device wire order; never repair backward timestamps
                # by sorting. Mixed-source power activity is deliberately ambiguous.
                for fd, (sec, usec, event_type, code, value) in events:
                    if event_type == 0 and code == 3:  # SYN_DROPPED
                        reset_state()
                        syncing[fd] = True
                        continue
                    if syncing[fd]:
                        if event_type == 0 and code == 0:  # SYN_REPORT
                            syncing[fd] = False
                            reset_state()
                        continue
                    if event_type != EV_KEY or code != KEY_POWER:
                        continue
                    # Duplicate/orphan releases never count and cannot invalidate
                    # feedback just completed by their own matching release.
                    if value == 0 and presses[fd] is None:
                        continue
                    now = time.monotonic()
                    captured = sec + usec / 1_000_000
                    if cancel_pending(now):
                        continue
                    if (not 0 <= usec < 1_000_000 or captured <= accept_after
                            or captured > now or (last_event[fd] is not None and captured < last_event[fd])):
                        reset_state()
                        continue
                    last_event[fd] = captured
                    if source_fd is not None and fd != source_fd:
                        reset_state()
                        continue
                    if value == 2:  # Autorepeat is not a new gesture.
                        continue
                    if value == 1:
                        if (presses[fd] is not None or (last_click is not None
                                and (captured <= last_click
                                     or captured - last_click < settings.debounce_seconds))):
                            reset_state()
                            continue
                        source_fd = fd
                        presses[fd] = captured
                    elif value == 0:
                        if presses[fd] is None:
                            continue
                        presses[fd] = None
                        last_click = captured
                        # ACPI reports instantaneous pairs even while physically
                        # held. A zero-duration pair is valid; never infer a hold.
                        count_click(captured)
                    else:
                        reset_state()
                if ready_action is not None:
                    action = ready_action
                    complete = (not any(buffered.values()) and not any(syncing.values())
                                and all(t is None for t in presses.values()))
                    reset_state()
                    if complete:
                        run_action(settings, action)
                        reset_state()
                now = time.monotonic()
                if (not cancel_pending(now) and not any(buffered.values())
                        and not any(syncing.values()) and all(t is None for t in presses.values())):
                    settle_sequence(now)
        except OSError:
            pass
        finally:
            reset_state()
            for fd in paths_by_fd:
                try:
                    selector.unregister(fd)
                except Exception:
                    pass
                try:
                    os.close(fd)
                except OSError:
                    pass
            selector.close()
            if running:
                time.sleep(1)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    args = parser.parse_args()

    settings = load_settings(args.config)
    if not settings.enabled:
        log(settings, "disabled by config")
        return 0

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log(
        settings,
        f"power sequences: {settings.reboot_click_count} clicks reboot, "
        f"{settings.poweroff_click_count} clicks poweroff, "
        f"{settings.confirm_click_count} clicks confirm",
    )
    return loop(settings)


if __name__ == "__main__":
    raise SystemExit(main())
