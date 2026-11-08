"""Optional offline per-tachometer diagnostics, not enabled by the PWM loop.

Continuous tachometer polling can add EC traffic; diagnostics are explicit.
"""
import os
from dataclasses import dataclass, field
from .common import read_text, set_led

FAN_FAULT_FLAG = "/run/proxnap-fan-fault"


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
