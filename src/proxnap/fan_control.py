#!/usr/bin/env python3
"""
Temperature-based fan controller for QNAP TVS-1288X on Proxmox.

The qnap8528 hwmon interface exposes conventional PWM values as 0..255. The
config keeps user-facing curves in duty percent 0..100 and this script scales
them before writing pwm1/pwm7.
"""

import argparse
import fcntl
import glob
import math
import os
import re
import signal
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field


DEFAULT_CONFIG = "/etc/proxnap/fan-control.conf"
HWMON_ROOT = "/sys/class/hwmon"
VALID_MODES = ("auto", "quiet", "normal", "extra", "full")
VALID_POLICIES = ("cpu_package", "storage", "storage_passive", "hdd", "system", "all")
GROUP_POLICIES = {
    "cpu": ("cpu_package", "all"),
    "rear": ("hdd", "storage", "storage_passive", "all"),
}
ACTUATION_LOCK = "/run/proxnap-fan-control.lock"
# Trusted fallback mapping, never derived from potentially invalid configuration.
SAFE_ZONES = (("cpu", "pwm7", 50.0), ("rear", "pwm1", 40.0))
HARDWARE_FLOORS = {"cpu": 50.0, "rear": 40.0}

running = True


@dataclass
class FanGroup:
    name: str
    pwm: str
    sensor_policy: str
    curve: list[tuple[float, float]]
    min_duty: float
    max_duty: float
    critical_temp: float
    manual_duty: float | None = None


@dataclass
class Settings:
    enabled: bool = True
    mode: str = "auto"
    interval: float = 5.0
    stop_duty: float = 100.0
    cpu_temp_ema_alpha: float = 0.25
    rear_temp_ema_alpha: float = 0.20
    cpu_temp_average_seconds: float = 0.0
    rear_temp_average_seconds: float = 0.0
    duty_hysteresis: float = 6.0
    min_increase_interval: float = 8.0
    min_decrease_interval: float = 25.0
    cpu_min_increase_interval: float | None = None
    cpu_min_decrease_interval: float | None = None
    rear_min_increase_interval: float | None = None
    rear_min_decrease_interval: float | None = None
    cpu_max_duty_step: float = 0.0
    rear_max_duty_step: float = 0.0
    cpu_emergency_temp: float = 92.0
    rear_emergency_temp: float = 65.0
    log_fan_rpm: bool = False
    pwm_refresh_seconds: float = 3600.0
    groups: list[FanGroup] | None = None


@dataclass
class ControlState:
    temp_ema: float | None = None
    temp_samples: list[tuple[float, float]] = field(default_factory=list)
    applied_duty: float | None = None
    last_change: float = 0.0
    last_pwm: int | None = None
    last_write: float = 0.0
    destination: tuple[str, str] | None = None
    readback_pwm: int | None = None
    write_attempted: bool = False


# Sensor inventory is deliberately retained across reloads for this process.
# A legitimate sensor removal requires operator review/restart, not silent adoption.
sensor_inventory: dict[str, set[str]] = {}
source_sensor_inventory: dict[str, set[str]] = {}
missing_source_paths: set[str] = set()
primary_source_seen = False
hwmon_cache: dict[str, list[str]] = {}
hwmon_cache_until = 0.0
cached_qnap_hwmon: str | None = None
fan_input_paths: list[str] = []
policy_temp_cache: dict[str, tuple[float, float | None]] = {}


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
    except OSError:
        return False


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def parse_bool(value: str, default: bool = False) -> bool:
    clean = value.strip().lower()
    if clean in {"1", "true", "yes", "on", "enabled"}:
        return True
    if clean in {"0", "false", "no", "off", "disabled"}:
        return False
    raise ValueError(f"invalid boolean: {value!r}")


def parse_float(value: str, default: float) -> float:
    number = float(value.strip())
    if not math.isfinite(number):
        raise ValueError(f"non-finite number: {value!r}")
    return number


def parse_optional_float(value: str) -> float | None:
    return parse_float(value, 0.0) if value.strip() else None


def parse_config_text(text: str) -> dict[str, str]:
    config: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            raise ValueError(f"invalid config line {number}")
        key, value = [part.strip() for part in line.split("=", 1)]
        if not key or key in config:
            raise ValueError(f"empty/duplicate config key: {key!r}")
        config[key] = value
    return config


def read_config(path: str) -> dict[str, str]:
    # Missing/unreadable config is an error, not a request for defaults.
    with open(path, "r", encoding="utf-8") as source:
        return parse_config_text(source.read())


def parse_curve(value: str, fallback: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not value.strip():
        return list(fallback)
    points = []
    for item in value.split(","):
        temp_s, duty_s = item.split(":")
        points.append((parse_float(temp_s, 0.0), parse_float(duty_s, 0.0)))
    if len(points) < 2:
        raise ValueError("curve needs at least two points")
    if any(not 0 <= temp < 130 or not 0 <= duty <= 100 for temp, duty in points):
        raise ValueError("curve point out of range")
    if any(t2 <= t1 or d2 < d1 for (t1, d1), (t2, d2) in zip(points, points[1:])):
        raise ValueError("curve temperatures must increase and duties must not decrease")
    if points[-1][1] != 100:
        raise ValueError("curve must end at 100%")
    return points


def parse_optional_duty(value: str) -> float | None:
    duty = parse_optional_float(value)
    if duty is not None and not 0 <= duty <= 100:
        raise ValueError("manual duty out of range")
    return duty


def profile_curves(mode: str) -> tuple[list[tuple[float, float]], list[tuple[float, float]], tuple[float, float], tuple[float, float]]:
    profiles = {
        "quiet": (
            [(45, 25), (60, 35), (70, 50), (80, 70), (90, 100)],
            [(38, 25), (48, 30), (55, 45), (62, 65), (70, 100)],
            (25, 100),
            (25, 100),
        ),
        "normal": (
            [(38, 28), (42, 30), (46, 32), (50, 35), (54, 39), (58, 44), (62, 50), (66, 58), (70, 67), (74, 76), (78, 84), (82, 92), (86, 100)],
            [(30, 28), (35, 30), (40, 33), (45, 38), (48, 43), (51, 49), (54, 55), (57, 62), (60, 70), (63, 78), (66, 88), (70, 100)],
            (28, 100),
            (28, 100),
        ),
        "extra": (
            [(35, 45), (50, 55), (60, 70), (70, 85), (80, 100)],
            [(30, 45), (40, 55), (48, 70), (55, 85), (62, 100)],
            (45, 100),
            (45, 100),
        ),
    }
    return profiles.get(mode, profiles["normal"])


def validate_raw_config(raw: dict[str, str]):
    bounds: dict[str, tuple[float, float]] = {
        "interval_seconds": (1, 3600), "stop_duty": (0, 100),
        "duty_hysteresis": (0, 30), "min_increase_interval": (0, 86400),
        "min_decrease_interval": (0, 86400), "pwm_refresh_seconds": (0, 86400),
    }
    for name, floor, critical_max, emergency_max in (("cpu", 50, 92, 92), ("rear", 40, 70, 65)):
        bounds.update({
            f"{name}_temp_ema_alpha": (0.05, 1),
            f"{name}_temp_average_seconds": (0, 86400),
            f"{name}_min_increase_interval": (0, 86400),
            f"{name}_min_decrease_interval": (0, 86400),
            f"{name}_max_duty_step": (0, 100),
            f"{name}_min_duty": (floor, 100), f"{name}_max_duty": (floor, 100),
            f"{name}_critical_temp": (1, critical_max),
            f"{name}_emergency_temp": (1, emergency_max),
            f"{name}_manual_duty": (0, 100),
        })
        expected_pwm = "pwm7" if name == "cpu" else "pwm1"
        if raw.get(f"{name}_pwm", expected_pwm) != expected_pwm:
            raise ValueError(f"{name} must use {expected_pwm} (physical mapping)")
        if raw.get(f"{name}_sensor_policy", "cpu_package" if name == "cpu" else "storage") not in GROUP_POLICIES[name]:
            raise ValueError(f"sensor policy does not cover the {name} thermal domain")
        curve = parse_curve(raw.get(f"{name}_curve", ""), [])
        if curve and curve[-1][0] > parse_float(raw.get(f"{name}_critical_temp", str(critical_max)), critical_max):
            raise ValueError(f"{name} curve reaches full duty after critical temperature")
        low = parse_float(raw.get(f"{name}_min_duty", str(floor)), floor)
        high = parse_float(raw.get(f"{name}_max_duty", "100"), 100)
        if low > high:
            raise ValueError(f"{name} min duty exceeds max duty")
    optional = {f"{name}_{suffix}" for name in ("cpu", "rear")
                for suffix in ("manual_duty", "min_increase_interval", "min_decrease_interval")}
    known = set(bounds) | {"enabled", "mode", "log_fan_rpm"} | {
        f"{name}_{suffix}" for name in ("cpu", "rear") for suffix in ("pwm", "sensor_policy", "curve")}
    if set(raw) - known:
        raise ValueError(f"unknown config keys: {sorted(set(raw) - known)}")
    for key, (low, high) in bounds.items():
        if key not in raw or (key in optional and not raw[key]):
            continue
        value = parse_float(raw[key], low)
        if not low <= value <= high:
            raise ValueError(f"{key} outside [{low}, {high}]")


def settings_from_config(path: str) -> Settings:
    return settings_from_raw(read_config(path))


def settings_from_raw(raw: dict[str, str]) -> Settings:
    validate_raw_config(raw)
    mode = raw.get("mode", "auto").strip().lower()
    if mode not in VALID_MODES:
        raise ValueError(f"invalid mode: {mode!r}")

    profile_mode = "normal" if mode in {"auto", "full"} else mode
    cpu_curve, rear_curve, cpu_limits, rear_limits = profile_curves(profile_mode)
    full_manual = 100.0 if mode == "full" else None
    custom_mode = mode == "auto"

    settings = Settings()
    settings.enabled = parse_bool(raw.get("enabled", "true"), True)
    settings.mode = mode
    settings.interval = max(1.0, parse_float(raw.get("interval_seconds", "5"), 5.0))
    settings.stop_duty = 100.0  # legacy stop_duty is parsed, never used to lower safety
    settings.cpu_temp_ema_alpha = clamp(parse_float(raw.get("cpu_temp_ema_alpha", "0.25"), 0.25), 0.05, 1.0)
    settings.rear_temp_ema_alpha = clamp(parse_float(raw.get("rear_temp_ema_alpha", "0.20"), 0.20), 0.05, 1.0)
    settings.cpu_temp_average_seconds = max(0.0, parse_float(raw.get("cpu_temp_average_seconds", "0"), 0.0))
    settings.rear_temp_average_seconds = max(0.0, parse_float(raw.get("rear_temp_average_seconds", "0"), 0.0))
    settings.duty_hysteresis = clamp(parse_float(raw.get("duty_hysteresis", "6"), 6.0), 0.0, 30.0)
    settings.min_increase_interval = max(0.0, parse_float(raw.get("min_increase_interval", "8"), 8.0))
    settings.min_decrease_interval = max(0.0, parse_float(raw.get("min_decrease_interval", "25"), 25.0))
    settings.cpu_min_increase_interval = parse_optional_float(raw.get("cpu_min_increase_interval", ""))
    settings.cpu_min_decrease_interval = parse_optional_float(raw.get("cpu_min_decrease_interval", ""))
    settings.rear_min_increase_interval = parse_optional_float(raw.get("rear_min_increase_interval", ""))
    settings.rear_min_decrease_interval = parse_optional_float(raw.get("rear_min_decrease_interval", ""))
    settings.cpu_max_duty_step = max(0.0, parse_float(raw.get("cpu_max_duty_step", "0"), 0.0))
    settings.rear_max_duty_step = max(0.0, parse_float(raw.get("rear_max_duty_step", "0"), 0.0))
    settings.cpu_emergency_temp = parse_float(raw.get("cpu_emergency_temp", "92"), 92.0)
    settings.rear_emergency_temp = parse_float(raw.get("rear_emergency_temp", "65"), 65.0)
    settings.log_fan_rpm = parse_bool(raw.get("log_fan_rpm", "false"), False)
    settings.pwm_refresh_seconds = max(0.0, parse_float(raw.get("pwm_refresh_seconds", "3600"), 3600.0))
    settings.groups = [
        FanGroup(
            name="cpu",
            pwm=raw.get("cpu_pwm", "pwm7"),
            sensor_policy=raw.get("cpu_sensor_policy", "cpu_package"),
            curve=parse_curve(raw.get("cpu_curve", ""), cpu_curve) if custom_mode else cpu_curve,
            min_duty=clamp(parse_float(raw.get("cpu_min_duty", str(cpu_limits[0])), cpu_limits[0]), 0.0, 100.0) if custom_mode else cpu_limits[0],
            max_duty=clamp(parse_float(raw.get("cpu_max_duty", str(cpu_limits[1])), cpu_limits[1]), 0.0, 100.0) if custom_mode else cpu_limits[1],
            critical_temp=parse_float(raw.get("cpu_critical_temp", "92"), 92.0),
            manual_duty=full_manual if full_manual is not None else (parse_optional_duty(raw.get("cpu_manual_duty", "")) if custom_mode else None),
        ),
        FanGroup(
            name="rear",
            pwm=raw.get("rear_pwm", "pwm1"),
            sensor_policy=raw.get("rear_sensor_policy", "storage"),
            curve=parse_curve(raw.get("rear_curve", ""), rear_curve) if custom_mode else rear_curve,
            min_duty=clamp(parse_float(raw.get("rear_min_duty", str(rear_limits[0])), rear_limits[0]), 0.0, 100.0) if custom_mode else rear_limits[0],
            max_duty=clamp(parse_float(raw.get("rear_max_duty", str(rear_limits[1])), rear_limits[1]), 0.0, 100.0) if custom_mode else rear_limits[1],
            critical_temp=parse_float(raw.get("rear_critical_temp", "70"), 70.0),
            manual_duty=full_manual if full_manual is not None else (parse_optional_duty(raw.get("rear_manual_duty", "")) if custom_mode else None),
        ),
    ]
    for group in settings.groups:
        group.min_duty = max(HARDWARE_FLOORS[group.name], group.min_duty)
    return settings


def hwmon_by_name(name: str) -> list[str]:
    global hwmon_cache, hwmon_cache_until, cached_qnap_hwmon, fan_input_paths

    now = time.monotonic()
    if now >= hwmon_cache_until:
        new_cache: dict[str, list[str]] = {}
        for path in sorted(glob.glob(os.path.join(HWMON_ROOT, "hwmon*"))):
            hw_name = read_file(os.path.join(path, "name"))
            if hw_name:
                new_cache.setdefault(hw_name, []).append(path)
        hwmon_cache = new_cache
        cached_qnap_hwmon = (hwmon_cache.get("qnap8528") or [None])[0]
        fan_input_paths = (
            sorted(glob.glob(os.path.join(cached_qnap_hwmon, "fan*_input")))
            if cached_qnap_hwmon
            else []
        )
        hwmon_cache_until = now + 60.0

    return hwmon_cache.get(name, [])


def read_temp_millic(path: str) -> float | None:
    raw = read_file(path)
    if not raw.lstrip("-").isdigit():
        return None
    value = int(raw)
    if value <= 0 or value >= 130000:
        return None
    return value / 1000.0


def tracked_temp(key: str, paths: list[str]) -> float | None:
    current = set(paths)
    known = sensor_inventory.setdefault(key, set())
    missing = known - current
    known.update(current)
    values = {path: None if path in missing_source_paths else read_temp_millic(path)
              for path in sorted(current)}
    failed = missing | {path for path, value in values.items() if value is None}
    if failed:
        print(f"ERROR: sensor loss/read failure {key}: {sorted(failed)}", flush=True)
        return None
    confirmed_values = [value for value in values.values() if value is not None]
    return max(confirmed_values) if confirmed_values else None


def inventory_paths(source: str, current_paths: list[str]) -> list[str]:
    # Share inventories between policies that consume the same sensor source;
    # a reload hdd -> storage must not hide a disk that already disappeared.
    current = set(current_paths)
    known = source_sensor_inventory.setdefault(source, set())
    missing_source_paths.difference_update(known)
    missing_source_paths.update(known - current)
    known.update(current)
    return sorted(known)


def coretemp_paths() -> list[str]:
    global primary_source_seen
    paths = []
    sources = hwmon_by_name("coretemp")
    if sources:
        primary_source_seen = True
    for hwmon in sources:
        for label_path in glob.glob(os.path.join(hwmon, "temp*_label")):
            if read_file(label_path) == "Package id 0":
                paths.append(label_path.replace("_label", "_input"))
    return inventory_paths("coretemp_package", paths)


def named_temp_paths(names: tuple[str, ...]) -> list[str]:
    paths = []
    for name in names:
        current = [path for hwmon in hwmon_by_name(name)
                   for path in sorted(glob.glob(os.path.join(hwmon, "temp*_input")))]
        paths.extend(inventory_paths(name, current))
    return paths


def qnap_temp_paths(indices: tuple[int, ...]) -> list[str]:
    return [path for index in indices for path in inventory_paths(
        f"qnap_temp{index}", [os.path.join(hwmon, f"temp{index}_input")
                            for hwmon in hwmon_by_name("qnap8528")])]


def coretemp_package() -> float | None:
    return tracked_temp("cpu_primary", coretemp_paths())


def qnap_temp(index: int) -> float | None:
    return tracked_temp(f"qnap_temp{index}", qnap_temp_paths((index,)))


def max_hwmon_temp(names: tuple[str, ...]) -> float | None:
    return tracked_temp("named:" + ",".join(names), named_temp_paths(names))


def storage_temp() -> float | None:
    # Never poll NVMe. Every path seen in this execution remains required,
    # including partial loss of one disk/EC sensor while others still work.
    paths = named_temp_paths(("drivetemp", "pch_cometlake", "jc42"))
    return tracked_temp("storage", paths + qnap_temp_paths((6, 7, 8)))


def storage_passive_temp() -> float | None:
    return max_hwmon_temp(("drivetemp", "pch_cometlake", "jc42"))


def hdd_temp() -> float | None:
    return max_hwmon_temp(("drivetemp",))


def policy_temp(policy: str) -> float | None:
    if policy == "cpu_package":
        temp = coretemp_package()
        # Even a discovered-but-unreadable primary source prohibits fallback.
        if primary_source_seen or sensor_inventory.get("cpu_primary"):
            return temp
        return qnap_temp(1)
    if policy == "storage":
        return storage_temp()
    if policy == "storage_passive":
        return storage_passive_temp()
    if policy == "hdd":
        return hdd_temp()
    if policy == "system":
        return tracked_temp("system", qnap_temp_paths((6, 7, 8)))
    if policy == "all":
        primary_paths = coretemp_paths()
        # An aggregate without an identifiable CPU package is not "all".
        # Do not mask an absent/unknown label with cooler disks or EC sensors,
        # including before the first successful primary inventory. No extra EC
        # fallback reads are introduced by this alternative policy.
        if not primary_paths:
            print("ERROR: all policy requires an identifiable CPU package sensor", flush=True)
            return None
        paths = primary_paths + named_temp_paths(("drivetemp", "pch_cometlake", "jc42", "acpitz"))
        return tracked_temp("all", paths + qnap_temp_paths((6, 7, 8)))
    return None


def cached_policy_temp(policy: str, max_age: float) -> float | None:
    now = time.monotonic()
    cached = policy_temp_cache.get(policy)
    if cached and now - cached[0] < max_age:
        return cached[1]
    temp = policy_temp(policy)
    policy_temp_cache[policy] = (now, temp)
    return temp


def duty_for_temp(temp: float, group: FanGroup) -> float:
    if temp >= group.critical_temp:
        return 100.0

    points = group.curve
    if temp <= points[0][0]:
        duty = points[0][1]
    elif temp >= points[-1][0]:
        duty = points[-1][1]
    else:
        duty = points[-1][1]
        for (t1, d1), (t2, d2) in zip(points, points[1:]):
            if t1 <= temp <= t2:
                ratio = (temp - t1) / (t2 - t1)
                duty = d1 + (d2 - d1) * ratio
                break

    return clamp(duty, max(HARDWARE_FLOORS[group.name], group.min_duty), group.max_duty)


def duty_to_pwm(duty: float) -> int:
    return round(clamp(duty, 0.0, 100.0) * 255 / 100)


def qnap_hwmon() -> str | None:
    paths = hwmon_by_name("qnap8528")
    return paths[0] if paths else None


def expected_readback(pwm: int) -> int:
    # EC stores integer percent then exports it on the 0..255 sysfs scale.
    return ((pwm * 100) // 255) * 255 // 100


def read_pwm(path: str) -> int | None:
    value = read_file(path)
    if not value.isdigit():
        return None
    number = int(value)
    return number if 0 <= number <= 255 else None


def initialize_state(hwmon: str, group: FanGroup, now: float) -> ControlState:
    path = os.path.join(hwmon, group.pwm)
    baseline = read_pwm(path)
    if baseline is None:
        raise RuntimeError(f"cannot read PWM baseline: {path}")
    # Baseline is not a confirmed command cache. Round up to avoid an abrupt
    # drop after restart/reload while the machine may still be cooling.
    return ControlState(applied_duty=max(group.min_duty, math.ceil(baseline * 100 / 255)),
                        last_change=now, destination=(hwmon, group.pwm))


def set_group_duty(hwmon: str, group: FanGroup, duty: float) -> bool:
    return maybe_set_group_duty(hwmon, group, duty, ControlState(), time.monotonic(), 0)


def maybe_set_group_duty(
    hwmon: str,
    group: FanGroup,
    duty: float,
    state: ControlState,
    now: float,
    refresh_seconds: float = 60.0,
) -> bool:
    state.write_attempted = False
    pwm = duty_to_pwm(duty)
    destination = (hwmon, group.pwm)
    if state.destination != destination:
        state.last_pwm = None
        state.readback_pwm = None
        state.destination = destination
    if state.last_pwm == pwm and now - state.last_write < refresh_seconds:
        # Same confirmed raw command also confirms an equivalent logical duty.
        if state.applied_duty != duty:
            state.applied_duty = duty
            state.last_change = now
        return True
    path = os.path.join(hwmon, group.pwm)
    state.write_attempted = True
    ok = write_file(path, str(pwm))
    readback = read_pwm(path) if ok else None
    if not ok or readback != expected_readback(pwm):
        state.last_pwm = None
        state.readback_pwm = None
        state.last_write = 0.0
        print(f"ERROR: PWM write/readback {path}: requested={pwm} "
              f"write_ok={ok} readback={readback} expected={expected_readback(pwm)}", flush=True)
        return False
    state.last_pwm = pwm
    state.readback_pwm = readback
    state.last_write = now
    if state.applied_duty != duty:
        state.last_change = now
    state.applied_duty = duty
    return True


def status_line(
    hwmon: str,
    mode: str,
    group: FanGroup,
    temp: float | None,
    control_temp: float | None,
    duty: float,
    log_fan_rpm: bool,
) -> str:
    fans = []
    if log_fan_rpm:
        paths = fan_input_paths if hwmon == cached_qnap_hwmon and fan_input_paths else sorted(glob.glob(os.path.join(hwmon, "fan*_input")))
        for path in paths:
            fans.append(f"{os.path.basename(path)}={read_file(path, '?')}")
    temp_s = "missing" if temp is None else f"{temp:.1f}C"
    control_s = ""
    if control_temp is not None and temp is not None and abs(control_temp - temp) >= 0.5:
        control_s = f" avg={control_temp:.1f}C"
    return f"configured_mode={mode} {group.name}: temp={temp_s}{control_s} requested_duty={duty:.0f}% requested_pwm={duty_to_pwm(duty)} {' '.join(fans)}"


def average_window_seconds(group: FanGroup, settings: Settings) -> float:
    return settings.cpu_temp_average_seconds if group.name == "cpu" else settings.rear_temp_average_seconds


def smoothed_temp(
    group: FanGroup,
    temp: float | None,
    settings: Settings,
    state: ControlState,
    now: float,
) -> float | None:
    if temp is None:
        return None
    window_seconds = average_window_seconds(group, settings)
    if window_seconds > 0:
        state.temp_samples.append((now, temp))
        cutoff = now - window_seconds
        state.temp_samples = [(ts, value) for ts, value in state.temp_samples if ts >= cutoff]
        state.temp_ema = sum(value for _, value in state.temp_samples) / len(state.temp_samples)
        return state.temp_ema

    alpha = settings.cpu_temp_ema_alpha if group.name == "cpu" else settings.rear_temp_ema_alpha
    if state.temp_ema is None:
        state.temp_ema = temp
    else:
        state.temp_ema = alpha * temp + (1.0 - alpha) * state.temp_ema
    return state.temp_ema


def emergency_temp(group: FanGroup, settings: Settings) -> float:
    return settings.cpu_emergency_temp if group.name == "cpu" else settings.rear_emergency_temp


def min_change_interval(group: FanGroup, settings: Settings, increasing: bool) -> float:
    if group.name == "cpu":
        override = settings.cpu_min_increase_interval if increasing else settings.cpu_min_decrease_interval
    else:
        override = settings.rear_min_increase_interval if increasing else settings.rear_min_decrease_interval
    if override is not None:
        return max(0.0, override)
    return settings.min_increase_interval if increasing else settings.min_decrease_interval


def max_duty_step(group: FanGroup, settings: Settings) -> float:
    return settings.cpu_max_duty_step if group.name == "cpu" else settings.rear_max_duty_step


def controlled_duty(
    group: FanGroup,
    raw_temp: float | None,
    target_duty: float,
    settings: Settings,
    state: ControlState,
    now: float,
) -> float:
    # Protections always precede manual/limits and may bypass any configured cap.
    if raw_temp is None or not math.isfinite(raw_temp) or raw_temp >= min(group.critical_temp, emergency_temp(group, settings)):
        return 100.0
    if group.manual_duty is not None:
        target_duty = clamp(group.manual_duty, max(HARDWARE_FLOORS[group.name], group.min_duty), group.max_duty)
    if state.applied_duty is None or target_duty >= state.applied_duty:
        # Thermal increases are immediate: neither hold, deadband nor step applies.
        return target_duty
    if now - state.last_change < min_change_interval(group, settings, False):
        return state.applied_duty
    # Even a decrease smaller than the legacy deadband must eventually reach
    # its target. State is committed only after the PWM write AND readback.
    step = max_duty_step(group, settings)
    return max(target_duty, state.applied_duty - step) if step > 0 else target_duty


@contextmanager
def exclusive_lock(path: str):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError(f"not a regular lock file: {path}")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(f"lock held: {path}") from error
        yield
    finally:
        os.close(fd)  # Never unlink a lock inode: other processes may hold it.


def _apply_safe_duty() -> int:
    # Caller holds ACTUATION_LOCK. Do not consult user-configured paths/stop duty.
    global hwmon_cache_until
    hwmon_cache_until = 0.0
    hwmon = qnap_hwmon()
    if not hwmon:
        print("ERROR: qnap8528 hwmon not found for full-duty failsafe", flush=True)
        return 1
    failed = False
    for name, pwm, floor in SAFE_ZONES:
        group = FanGroup(name, pwm, "", [], floor, 100, 0)
        # Do not short-circuit: attempt both zones even if the first fails.
        try:
            if not set_group_duty(hwmon, group, 100.0):
                failed = True
        except Exception as error:
            print(f"ERROR: failsafe {name}: {error}", flush=True)
            failed = True
    return int(failed)


def apply_safe_duty(config_path: str = DEFAULT_CONFIG) -> int:
    # Public helper has the same exclusion contract as CLI --safe.
    try:
        with exclusive_lock(ACTUATION_LOCK):
            return _apply_safe_duty()
    except Exception as error:
        print(f"ERROR: failsafe: {error}", flush=True)
        return 1


def set_config_value(path: str, key: str, value: str):
    if key != "mode" or value not in VALID_MODES:
        raise ValueError("only a valid fan mode may be edited")
    if stat.S_ISLNK(os.lstat(path).st_mode):
        raise ValueError("refusing to edit a symlink config")
    with exclusive_lock(path + ".lock"):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8", newline="") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("config is not a regular file")
            original = source.read()
        settings_from_raw(parse_config_text(original))
        changed = False
        result = []
        for line in original.splitlines(keepends=True):
            body = line.rstrip("\r\n")
            ending = line[len(body):]
            content, marker, comment = body.partition("#")
            match = re.match(r"^(\s*mode\s*=\s*)(.*)$", content)
            if match:
                tail = match[2][len(match[2].rstrip()):]
                body = match[1] + value + tail + marker + comment
                changed = True
            result.append(body + ending)
        if not changed:
            if result and not result[-1].endswith("\n"):
                result[-1] += "\n"
            result.append(f"mode={value}\n")
        replacement = "".join(result)
        settings_from_raw(parse_config_text(replacement))
        temp_path = None
        try:
            fd, temp_path = tempfile.mkstemp(prefix=".fan-control-", dir=os.path.dirname(os.path.abspath(path)))
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as target:
                if (os.fstat(target.fileno()).st_uid, os.fstat(target.fileno()).st_gid) != (metadata.st_uid, metadata.st_gid):
                    os.fchown(target.fileno(), metadata.st_uid, metadata.st_gid)
                os.fchmod(target.fileno(), stat.S_IMODE(metadata.st_mode))
                target.write(replacement)
                target.flush()
                os.fsync(target.fileno())
            current = os.lstat(path)
            if (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_ctime_ns) != (metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_ctime_ns):
                raise RuntimeError("config changed during edit")
            os.replace(temp_path, path)
            temp_path = None
            directory = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temp_path is not None:
                os.unlink(temp_path)


def run_controller(args) -> int:
    global hwmon_cache_until
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    last_log = 0.0
    last_context = None
    last_settings = None
    states: dict[str, ControlState] = {}
    while running:
        settings = settings_from_config(args.config)
        hwmon = qnap_hwmon()
        if not hwmon:
            raise RuntimeError("qnap8528 hwmon not found")
        # Clear sensor result caches on reload/remap, never the observed inventory.
        context = (repr(settings), hwmon, repr(hwmon_cache))
        if context != last_settings:
            policy_temp_cache.clear()
            last_settings = context
        if not settings.enabled:
            result = _apply_safe_duty()
            states.clear()
            last_context = None
            if args.once:
                return result
            time.sleep(settings.interval)
            continue
        now = time.monotonic()
        temps = {group.name: cached_policy_temp(group.sensor_policy, 5.0 if group.sensor_policy == "cpu_package" else 20.0)
                 for group in settings.groups or []}
        context += (repr(sensor_inventory),)
        if context != last_context:
            states = {group.name: initialize_state(hwmon, group, now) for group in settings.groups or []}
            last_context = context
        should_log = args.once or now - last_log >= 30
        lines = []
        failed = False
        for group in settings.groups or []:
            state = states[group.name]
            temp = temps[group.name]
            control_temp = smoothed_temp(group, temp, settings, state, now)
            if temp is None:
                target = 100.0
                failed = True
                print(f"ERROR: {group.name} sensor unavailable; requesting 100%", flush=True)
            else:
                target = max(duty_for_temp(temp, group), duty_for_temp(control_temp if control_temp is not None else temp, group))
            duty = controlled_duty(group, temp, target, settings, state, now)
            ok = maybe_set_group_duty(hwmon, group, duty, state, now, settings.pwm_refresh_seconds)
            if not ok:
                failed = True
            if should_log:
                lines.append(status_line(hwmon, settings.mode, group, temp, control_temp, duty, settings.log_fan_rpm)
                             + f" write_attempted={state.write_attempted} confirmed={ok} readback={state.readback_pwm}")
        if should_log:
            print(" | ".join(lines), flush=True)
            last_log = now
        if failed:
            # Broader runtime failure fallback uses the trusted map and also
            # invalidates caches so the next loop cannot simulate a retry.
            _apply_safe_duty()
            states.clear()
            last_context = None
            hwmon_cache_until = 0.0
        if args.once:
            # A stop signal received during a one-shot must not bypass shutdown
            # full duty just because the normal pass happened to finish first.
            return max(int(failed), _apply_safe_duty() if not running else 0)
        if not running:
            break
        time.sleep(settings.interval)
    return _apply_safe_duty()


def main(argv=None) -> int:
    global running
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--once", action="store_true")
    actions.add_argument("--safe", action="store_true", help="request and verify 100% on both zones, then exit")
    actions.add_argument("--set-mode", choices=VALID_MODES, help="atomically persist fan mode and exit")
    actions.add_argument("--check-config", action="store_true", help="validate config without lock or hwmon IO")
    args = parser.parse_args(argv)
    if args.check_config or args.set_mode:
        try:
            if args.set_mode:
                set_config_value(args.config, "mode", args.set_mode)
                print(f"mode={args.set_mode}", flush=True)
            else:
                settings_from_config(args.config)
                print("config valid (no hardware IO)", flush=True)
            return 0
        except Exception as error:
            print(f"ERROR: config: {error}", flush=True)
            return 1
    try:
        with exclusive_lock(ACTUATION_LOCK):
            try:
                if args.safe:
                    return _apply_safe_duty()
                running = True
                return run_controller(args)
            except Exception as error:
                print(f"ERROR: controller: {error}", flush=True)
                _apply_safe_duty()
                return 1
    except Exception as error:
        # A held/unusable lock must NEVER cause unprotected failsafe writes.
        print(f"ERROR: actuation lock/failsafe: {error}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
