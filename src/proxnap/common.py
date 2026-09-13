from __future__ import annotations

import fcntl
import glob
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

HWMON_ROOT = "/sys/class/hwmon"
LED_ROOT = "/sys/class/leds"
EC_LOCK = "/run/lock/proxnap-ec.lock"
LCD_LOCK = "/run/lock/proxnap-lcd.lock"
ACTION_LOCK = "/run/lock/proxnap-action.lock"
DMI_PRODUCT_NAME = "/sys/class/dmi/id/product_name"
QNAP_VPD_ROOT = "/sys/devices/platform/qnap8528/vpd"
SUPPORTED_PRODUCT = "TVS-1288X"
SUPPORTED_MB = "Q05W0"
SUPPORTED_BP = "Q05K0"
GENERIC_DMI_PRODUCTS = {
    "",
    "DEFAULT STRING",
    "SYSTEM PRODUCT NAME",
    "TO BE FILLED BY O.E.M.",
    "UNKNOWN",
}


def read_text(path: str | os.PathLike[str], default: str = "") -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return default


def write_text(path: str | os.PathLike[str], value: str, verify: bool = False) -> bool:
    try:
        Path(path).write_text(value, encoding="utf-8")
        return not verify or read_text(path) == value
    except OSError:
        return False


def read_config(path: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in read_text(path).splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        result[key] = value
    return result


def set_config_value(path: str, key: str, value: str) -> None:
    """Set one key while preserving a valid newline-delimited config file."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    output: list[str] = []
    found = False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            output.append(f"{key}={value}")
            found = True
        else:
            output.append(line)
    if not found:
        output.append(f"{key}={value}")
    Path(path).write_text("\n".join(output) + "\n", encoding="utf-8")


def model_is_supported(
    dmi_path: str = DMI_PRODUCT_NAME,
    vpd_root: str = QNAP_VPD_ROOT,
) -> bool:
    """Accept only the tested TVS-1288X identity.

    Some units expose a generic DMI product name, so the qnap8528 VPD
    mainboard/backplane pair is the authoritative fallback. The TVS-1688X uses
    different codes and is deliberately rejected.
    """
    product = read_text(dmi_path).upper()
    if product in {"TVS-1288X", "TVS-H1288X"}:
        return True
    # A specific, unsupported DMI identity always wins over VPD. This prevents
    # conflicting evidence from allowing the untested TVS-h1688X.
    if product not in GENERIC_DMI_PRODUCTS:
        return False
    mainboard = read_text(os.path.join(vpd_root, "mainboard_model")).upper().replace("-", "")
    backplane = read_text(os.path.join(vpd_root, "backplane_model")).upper().replace("-", "")
    return mainboard == SUPPORTED_MB and backplane == SUPPORTED_BP


def require_supported_model(
    dmi_path: str = DMI_PRODUCT_NAME,
    vpd_root: str = QNAP_VPD_ROOT,
) -> bool:
    if model_is_supported(dmi_path, vpd_root):
        return True
    print(
        "HARDWARE REFUSED: only the tested TVS-1288X "
        f"({SUPPORTED_MB}/{SUPPORTED_BP}) is accepted",
        flush=True,
    )
    return False


def parse_bool(value: str, default: bool = False) -> bool:
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


@contextmanager
def exclusive_lock(path: str, timeout: float = 5.0) -> Iterator[None]:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=0o755, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"lock timeout: {path}")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def hwmon_paths(name: str, root: str = HWMON_ROOT) -> list[str]:
    return [
        path
        for path in sorted(glob.glob(os.path.join(root, "hwmon*")))
        if read_text(os.path.join(path, "name")) == name
    ]


def valid_temp(path: str) -> float | None:
    raw = read_text(path)
    if not raw.lstrip("-").isdigit():
        return None
    value = int(raw)
    if not 0 < value < 130_000:
        return None
    return value / 1000.0


def set_led(name: str, brightness: int, root: str = LED_ROOT) -> bool:
    base = os.path.join(root, f"qnap8528::{name}")
    if not os.path.isdir(base):
        return False
    with exclusive_lock(EC_LOCK):
        trigger_ok = write_text(os.path.join(base, "trigger"), "none")
        bright_ok = write_text(os.path.join(base, "brightness"), str(brightness), verify=True)
    return trigger_ok and bright_ok
