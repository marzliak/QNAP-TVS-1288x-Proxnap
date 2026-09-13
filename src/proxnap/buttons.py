from __future__ import annotations

import argparse
import os
import selectors
import signal
import struct
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

from .common import (
    ACTION_LOCK,
    exclusive_lock,
    parse_bool,
    parse_float,
    parse_int,
    read_config,
    require_supported_model,
)

DEFAULT_COPY_CONFIG = "/etc/proxnap/copy-button.conf"
DEFAULT_POWER_CONFIG = "/etc/proxnap/power-button.conf"
EVENT = struct.Struct("llHHi")
EV_KEY = 1
KEY_POWER = 116
BTN_COPY = 0x102
RUNNING = True


def stop(_signum=None, _frame=None) -> None:
    global RUNNING
    RUNNING = False


@dataclass(frozen=True)
class Policy:
    enabled: bool
    input_paths: tuple[str, ...]
    key_code: int
    refresh_count: int
    upgrade_count: int
    reboot_count: int
    poweroff_count: int
    confirm_count: int
    sequence_window: float
    settle_seconds: float
    confirm_window: float
    apt_refresh_enabled: bool
    apt_upgrade_enabled: bool
    reboot_enabled: bool
    poweroff_enabled: bool


def load_policy(path: str, kind: str) -> Policy:
    raw = read_config(path)
    copy = kind == "copy"
    default_paths = "/dev/input/by-path/platform-qnap8528-event" if copy else "/dev/input/by-path/platform-PNP0C0C:00-event,/dev/input/by-path/platform-LNXPWRBN:00-event"
    return Policy(
        enabled=parse_bool(raw.get("enabled", "false"), False),
        input_paths=tuple(item.strip() for item in raw.get("input_paths", default_paths).split(",") if item.strip()),
        key_code=parse_int(raw.get("key_code", hex(BTN_COPY if copy else KEY_POWER)), BTN_COPY if copy else KEY_POWER),
        refresh_count=max(2, parse_int(raw.get("refresh_click_count", "3"), 3)),
        upgrade_count=max(3, parse_int(raw.get("upgrade_click_count", "5"), 5)),
        reboot_count=max(4, parse_int(raw.get("reboot_click_count", "8"), 8)),
        poweroff_count=max(5, parse_int(raw.get("poweroff_click_count", "12"), 12)),
        confirm_count=max(2, parse_int(raw.get("confirm_click_count", "3"), 3)),
        sequence_window=max(1.0, parse_float(raw.get("sequence_window_seconds", "5"), 5.0)),
        settle_seconds=max(0.2, parse_float(raw.get("sequence_settle_seconds", "1"), 1.0)),
        confirm_window=max(2.0, parse_float(raw.get("confirm_window_seconds", "5"), 5.0)),
        apt_refresh_enabled=parse_bool(raw.get("apt_refresh_enabled", "false"), False),
        apt_upgrade_enabled=parse_bool(raw.get("apt_upgrade_enabled", "false"), False),
        reboot_enabled=parse_bool(raw.get("reboot_enabled", "false"), False),
        poweroff_enabled=parse_bool(raw.get("poweroff_enabled", "false"), False),
    )


def action_for_count(count: int, policy: Policy, kind: str) -> str | None:
    if count >= policy.poweroff_count:
        return "poweroff"
    if count >= policy.reboot_count:
        return "reboot"
    if kind == "copy" and count >= policy.upgrade_count:
        return "upgrade"
    if kind == "copy" and count >= policy.refresh_count:
        return "refresh"
    return None


def action_allowed(action: str, policy: Policy) -> bool:
    return {
        "refresh": policy.apt_refresh_enabled,
        "upgrade": policy.apt_upgrade_enabled,
        "reboot": policy.reboot_enabled,
        "poweroff": policy.poweroff_enabled,
    }.get(action, False)


def _run_command(command: list[str]) -> int:
    return subprocess.run(command, check=False).returncode


def execute_action(
    action: str,
    policy: Policy,
    *,
    runner: Callable[[list[str]], int] | None = None,
    identity_check: Callable[[], bool] = require_supported_model,
) -> int:
    if not action_allowed(action, policy):
        print(f"ACTION BLOCKED: {action} is disabled in configuration", flush=True)
        return 3
    if not identity_check():
        print(f"ACTION REFUSED: {action} on unsupported hardware", flush=True)
        return 4
    commands = {
        "refresh": ["apt-get", "update"],
        "upgrade": ["apt-get", "-y", "-o", "Dpkg::Options::=--force-confold", "dist-upgrade"],
        "reboot": ["systemctl", "reboot"],
        "poweroff": ["systemctl", "poweroff"],
    }
    selected = commands.get(action)
    if selected is None:
        print(f"ACTION REFUSED: {action} is not allow-listed", flush=True)
        return 4
    final_runner = runner or _run_command
    with exclusive_lock(ACTION_LOCK, timeout=1.0):
        print(f"ACTION EXECUTING: {action}", flush=True)
        return final_runner(selected)


def deduplicate_input_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    """Collapse aliases that resolve to the same input device."""
    result: list[str] = []
    identities: set[tuple[int, int, int] | tuple[str, str]] = set()
    for path in paths:
        try:
            info = os.stat(path)
            identity: tuple[int, int, int] | tuple[str, str] = (
                info.st_dev,
                info.st_ino,
                info.st_rdev,
            )
        except OSError:
            identity = ("missing", os.path.realpath(path))
        if identity in identities:
            continue
        identities.add(identity)
        result.append(path)
    return tuple(result)


def read_events(fd: int, key_code: int) -> int:
    data = os.read(fd, EVENT.size * 32)
    count = 0
    for offset in range(0, len(data) - EVENT.size + 1, EVENT.size):
        _sec, _usec, event_type, code, value = EVENT.unpack_from(data, offset)
        if event_type == EV_KEY and code == key_code and value == 0:
            count += 1
    return count


def run(path: str, kind: str) -> int:
    policy = load_policy(path, kind)
    if not policy.enabled:
        print(f"{kind} button disabled by configuration", flush=True)
        return 0
    selector = selectors.DefaultSelector()
    handles: list[int] = []
    for input_path in deduplicate_input_paths(policy.input_paths):
        try:
            fd = os.open(input_path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            print(f"INPUT unavailable: {input_path}", flush=True)
            continue
        selector.register(fd, selectors.EVENT_READ)
        handles.append(fd)
    if not handles:
        selector.close()
        return 1

    clicks: list[float] = []
    confirms: list[float] = []
    pending: str | None = None
    pending_until = 0.0
    try:
        while RUNNING:
            now = time.monotonic()
            if pending and now > pending_until:
                print(f"ACTION CANCELLED: {pending} confirmation timeout", flush=True)
                pending = None
                confirms.clear()
            if not pending and clicks and now - clicks[-1] >= policy.settle_seconds:
                clicks = [stamp for stamp in clicks if now - stamp <= policy.sequence_window]
                action = action_for_count(len(clicks), policy, kind)
                print(f"BUTTON sequence: {len(clicks)} click(s), action={action or 'none'}", flush=True)
                clicks.clear()
                if action:
                    if not action_allowed(action, policy):
                        print(f"ACTION BLOCKED: {action} is disabled in configuration", flush=True)
                    else:
                        pending = action
                        pending_until = now + policy.confirm_window
                        confirms.clear()
                        print(f"ACTION ARMED: {action}; {policy.confirm_count} confirmation clicks required", flush=True)
            for key, _mask in selector.select(timeout=0.2):
                try:
                    releases = read_events(key.fd, policy.key_code)
                except (BlockingIOError, OSError):
                    continue
                for _ in range(releases):
                    stamp = time.monotonic()
                    if pending:
                        confirms.append(stamp)
                        confirms = [value for value in confirms if stamp - value <= policy.confirm_window]
                        if len(confirms) >= policy.confirm_count:
                            action = pending
                            pending = None
                            confirms.clear()
                            execute_action(action, policy)
                    else:
                        clicks.append(stamp)
                        clicks = [value for value in clicks if stamp - value <= policy.sequence_window]
    finally:
        for fd in handles:
            if fd in selector.get_map():
                selector.unregister(fd)
            os.close(fd)
        selector.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Guarded TVS-1288X physical button handler")
    parser.add_argument("--kind", choices=("copy", "power"), required=True)
    parser.add_argument("--config")
    args = parser.parse_args()
    if not require_supported_model():
        return 4
    config = args.config or (DEFAULT_COPY_CONFIG if args.kind == "copy" else DEFAULT_POWER_CONFIG)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return run(config, args.kind)


if __name__ == "__main__":
    raise SystemExit(main())
