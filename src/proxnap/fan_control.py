from __future__ import annotations

import argparse
import glob
import os
import signal
import time
from dataclasses import dataclass, field
from itertools import pairwise

from .common import (
    EC_LOCK,
    exclusive_lock,
    hwmon_paths,
    parse_float,
    parse_int,
    read_config,
    read_text,
    require_supported_model,
    set_led,
    valid_temp,
    write_text,
)

DEFAULT_CONFIG = "/etc/proxnap/fan-control.conf"
FAN_FAULT_FLAG = "/run/proxnap-fan-fault"
RUNNING = True


@dataclass(frozen=True)
class FanZone:
    name: str
    pwm: str
    fan_inputs: tuple[str, ...]
    sensor_policy: str
    curve: tuple[tuple[float, float], ...]
    minimum: float
    critical: float


@dataclass
class ZoneState:
    duty: float | None = None
    failed_rpm_cycles: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Settings:
    interval: float
    hysteresis: float
    stall_cycles: int
    cpu: FanZone
    rear: FanZone


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def duty_to_pwm(duty: float) -> int:
    return round(clamp(duty, 0.0, 100.0) * 255.0 / 100.0)


def parse_curve(value: str, fallback: tuple[tuple[float, float], ...]) -> tuple[tuple[float, float], ...]:
    points: list[tuple[float, float]] = []
    for item in value.split(","):
        if ":" not in item:
            continue
        left, right = (part.strip() for part in item.split(":", 1))
        try:
            points.append((float(left), clamp(float(right), 0.0, 100.0)))
        except ValueError:
            continue
    points.sort()
    if len(points) < 2 or any(a[0] >= b[0] for a, b in pairwise(points)):
        return fallback
    return tuple(points)


def load_settings(path: str) -> Settings:
    raw = read_config(path)
    cpu_default = ((0.0, 50.0), (85.0, 50.0), (88.0, 65.0), (90.0, 80.0), (92.0, 100.0))
    rear_default = ((0.0, 40.0), (55.0, 40.0), (60.0, 55.0), (63.0, 75.0), (65.0, 100.0))
    cpu_min = clamp(parse_float(raw.get("cpu_min_duty", "50"), 50.0), 20.0, 100.0)
    rear_min = clamp(parse_float(raw.get("rear_min_duty", "40"), 40.0), 20.0, 100.0)
    cpu = FanZone(
        "cpu", "pwm7", ("fan7_input", "fan8_input"),
        raw.get("cpu_sensor_policy", "cpu_package"),
        parse_curve(raw.get("cpu_curve", ""), cpu_default), cpu_min,
        parse_float(raw.get("cpu_critical_temp", "92"), 92.0),
    )
    rear = FanZone(
        "rear", "pwm1", ("fan1_input", "fan2_input", "fan3_input"),
        raw.get("rear_sensor_policy", "storage"),
        parse_curve(raw.get("rear_curve", ""), rear_default), rear_min,
        parse_float(raw.get("rear_critical_temp", "70"), 70.0),
    )
    allowed = {"cpu_package", "storage", "hdd", "system"}
    if cpu.sensor_policy not in allowed or rear.sensor_policy not in allowed:
        raise ValueError("unsupported sensor policy")
    return Settings(
        interval=max(1.0, parse_float(raw.get("interval_seconds", "5"), 5.0)),
        hysteresis=clamp(parse_float(raw.get("duty_hysteresis", "4"), 4.0), 0.0, 20.0),
        stall_cycles=max(1, parse_int(raw.get("stall_cycles", "3"), 3)),
        cpu=cpu,
        rear=rear,
    )


def duty_for_temp(temp: float | None, zone: FanZone) -> float:
    if temp is None or temp >= zone.critical:
        return 100.0
    points = zone.curve
    if temp <= points[0][0]:
        return max(zone.minimum, points[0][1])
    for (t1, d1), (t2, d2) in pairwise(points):
        if t1 <= temp <= t2:
            ratio = (temp - t1) / (t2 - t1)
            return clamp(d1 + (d2 - d1) * ratio, zone.minimum, 100.0)
    return clamp(points[-1][1], zone.minimum, 100.0)


def package_temp(root: str = "/sys/class/hwmon") -> float | None:
    values: list[float] = []
    for base in hwmon_paths("coretemp", root):
        for label in glob.glob(os.path.join(base, "temp*_label")):
            if read_text(label) == "Package id 0":
                value = valid_temp(label.replace("_label", "_input"))
                if value is not None:
                    values.append(value)
    return max(values) if values else None


def qnap_temp(indices: tuple[int, ...], root: str = "/sys/class/hwmon") -> float | None:
    values: list[float] = []
    for base in hwmon_paths("qnap8528", root):
        for index in indices:
            value = valid_temp(os.path.join(base, f"temp{index}_input"))
            if value is not None:
                values.append(value)
    return max(values) if values else None


def named_hwmon_temp(names: tuple[str, ...], root: str = "/sys/class/hwmon") -> float | None:
    values: list[float] = []
    for name in names:
        for base in hwmon_paths(name, root):
            for path in glob.glob(os.path.join(base, "temp*_input")):
                value = valid_temp(path)
                if value is not None:
                    values.append(value)
    return max(values) if values else None


def sensor_temp(policy: str, root: str = "/sys/class/hwmon") -> float | None:
    if policy == "cpu_package":
        return package_temp(root) or qnap_temp((1,), root)
    if policy == "hdd":
        return named_hwmon_temp(("drivetemp",), root)
    if policy == "system":
        return qnap_temp((6, 7, 8), root)
    if policy == "storage":
        values = [named_hwmon_temp(("drivetemp", "pch_cometlake", "jc42"), root), qnap_temp((6, 7, 8), root)]
        present = [value for value in values if value is not None]
        return max(present) if present else None
    return None


def find_qnap_hwmon(root: str = "/sys/class/hwmon") -> str | None:
    paths = hwmon_paths("qnap8528", root)
    return paths[0] if paths else None


def write_zone(hwmon: str, zone: FanZone, duty: float) -> bool:
    value = str(duty_to_pwm(duty))
    return write_text(os.path.join(hwmon, zone.pwm), value, verify=True)


def force_full_speed(hwmon: str | None = None) -> bool:
    hwmon = hwmon or find_qnap_hwmon()
    if not hwmon:
        return False
    ok = True
    try:
        with exclusive_lock(EC_LOCK):
            for pwm in ("pwm1", "pwm7"):
                ok = write_text(os.path.join(hwmon, pwm), "255", verify=True) and ok
    except (OSError, TimeoutError):
        return False
    return ok


def mark_fault(message: str) -> None:
    print(f"FAN FAULT: {message}", flush=True)
    try:
        os.makedirs(os.path.dirname(FAN_FAULT_FLAG), exist_ok=True)
        with open(FAN_FAULT_FLAG, "w", encoding="utf-8") as handle:
            handle.write(message + "\n")
    except OSError:
        pass
    try:
        set_led("status", 2)
    except (OSError, TimeoutError):
        pass


def clear_fault() -> None:
    try:
        os.unlink(FAN_FAULT_FLAG)
    except FileNotFoundError:
        pass


def rpm_stalled(hwmon: str, zone: FanZone, state: ZoneState, duty: float, threshold: int) -> bool:
    """Return true when any expected tachometer is absent or stays at zero."""
    if duty <= 0:
        state.failed_rpm_cycles.clear()
        return False

    stalled = False
    for name in zone.fan_inputs:
        path = os.path.join(hwmon, name)
        raw = read_text(path) if os.path.exists(path) else ""
        rpm = int(raw) if raw.isdigit() else 0
        if rpm > 0:
            state.failed_rpm_cycles[name] = 0
            continue
        cycles = state.failed_rpm_cycles.get(name, 0) + 1
        state.failed_rpm_cycles[name] = cycles
        stalled = stalled or cycles >= threshold
    return stalled


def requested_duties(
    settings: Settings,
    samples: dict[str, float | None],
    states: dict[str, ZoneState],
) -> tuple[dict[str, float], bool]:
    sensor_missing = any(value is None for value in samples.values())
    if sensor_missing:
        # A missing source is treated as a chassis-wide loss of thermal
        # visibility: both zones go to 100%, not only the affected zone.
        return {"cpu": 100.0, "rear": 100.0}, True
    requested: dict[str, float] = {}
    for zone in (settings.cpu, settings.rear):
        state = states[zone.name]
        target = duty_for_temp(samples[zone.name], zone)
        if state.duty is not None and abs(target - state.duty) < settings.hysteresis:
            target = state.duty
        requested[zone.name] = target
    return requested, False


def stop(_signum=None, _frame=None) -> None:
    global RUNNING
    RUNNING = False


def run(config_path: str, once: bool = False) -> int:
    states = {"cpu": ZoneState(), "rear": ZoneState()}
    hwmon = find_qnap_hwmon()
    if not hwmon:
        mark_fault("qnap8528 hwmon not found; PWM control unavailable")
        return 1
    try:
        while RUNNING:
            settings = load_settings(config_path)
            samples = {
                "cpu": sensor_temp(settings.cpu.sensor_policy),
                "rear": sensor_temp(settings.rear.sensor_policy),
            }
            requested, sensor_missing = requested_duties(settings, samples, states)

            with exclusive_lock(EC_LOCK):
                results = [write_zone(hwmon, zone, requested[zone.name]) for zone in (settings.cpu, settings.rear)]
            if not all(results):
                force_full_speed(hwmon)
                mark_fault("PWM write/readback failed; requested 100% on both zones")
                return 1

            stalled = False
            for zone in (settings.cpu, settings.rear):
                states[zone.name].duty = requested[zone.name]
                if rpm_stalled(hwmon, zone, states[zone.name], requested[zone.name], settings.stall_cycles):
                    stalled = True
                    mark_fault(f"{zone.name} RPM stayed at zero; requested 100% on both zones")
            if stalled:
                force_full_speed(hwmon)
                return 1

            if sensor_missing:
                missing = ",".join(name for name, value in samples.items() if value is None)
                mark_fault(f"temperature sensor missing ({missing}); both zones at 100%")
            else:
                clear_fault()
            print(" | ".join(
                f"{zone.name}: temp={'missing' if samples[zone.name] is None else f'{samples[zone.name]:.1f}C'} duty={requested[zone.name]:.0f}%"
                for zone in (settings.cpu, settings.rear)
            ), flush=True)
            if once:
                return 0
            time.sleep(settings.interval)
    except Exception as exc:  # noqa: BLE001 - unexpected failures must trigger thermal fail-safe
        force_full_speed(hwmon)
        mark_fault(f"controller error ({type(exc).__name__}); requested 100% on both zones")
        return 1
    finally:
        if not RUNNING and not force_full_speed(hwmon):
            mark_fault("stop fallback could not verify 100% PWM")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="TVS-1288X two-zone fan controller")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--failsafe", action="store_true", help="set pwm1 and pwm7 to 100% and exit")
    args = parser.parse_args()
    if not require_supported_model():
        return 4
    if args.failsafe:
        ok = force_full_speed()
        if not ok:
            mark_fault("manual failsafe could not verify 100% PWM")
        return 0 if ok else 1
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return run(args.config, args.once)


if __name__ == "__main__":
    raise SystemExit(main())
