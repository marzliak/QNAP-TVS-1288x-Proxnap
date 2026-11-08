"""Legacy ACPI cooling-state helpers; not PWM control and never auto-enabled.

No service or command invokes this experimental policy. It is retained only
for offline analysis on platforms without PWM; not a thermal protection claim.
"""
import glob
import os
import sys
from dataclasses import dataclass
FAN_ACTIVE_TEMP_C = 70
FAN_IDLE_TEMP_C = 55

def read_file(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return default

def write_file(path: str, value: str) -> bool:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(value)
        return True
    except Exception as exc:
        print(f"WARN: cannot write {path}: {exc}", file=sys.stderr)
        return False

@dataclass
class FanState:
    devices: list[str]
    active: bool = False

    @classmethod
    def discover(cls):
        devices = []
        for path in sorted(glob.glob("/sys/class/thermal/cooling_device*")):
            if read_file(os.path.join(path, "type")) == "Fan":
                devices.append(path)
        return cls(devices)

    def set_active(self, active: bool):
        value = "1" if active else "0"
        for path in self.devices:
            write_file(os.path.join(path, "cur_state"), value)
        self.active = active

    def describe(self) -> str:
        states = []
        for path in self.devices:
            states.append(read_file(os.path.join(path, "cur_state"), "?"))
        return "".join(states) if states else "none"

    def apply_policy(self, temp_c: int):
        if not self.devices:
            return
        if temp_c >= FAN_ACTIVE_TEMP_C and not self.active:
            self.set_active(True)
            print(f"fan acpi active: temp={temp_c}C devices={len(self.devices)}")
        elif temp_c <= FAN_IDLE_TEMP_C and self.active:
            self.set_active(False)
            print(f"fan acpi idle: temp={temp_c}C devices={len(self.devices)}")
