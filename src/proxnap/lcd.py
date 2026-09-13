from __future__ import annotations

import argparse
import glob
import os
import select
import signal
import termios
import time
from dataclasses import dataclass

from .common import (
    LCD_LOCK,
    exclusive_lock,
    parse_float,
    read_config,
    read_text,
    require_supported_model,
    valid_temp,
)

DEFAULT_CONFIG = "/etc/proxnap/lcd.conf"
DEFAULT_PORT = "/dev/ttyS1"
WIDTH = 16
RUNNING = True


def stop(_signum=None, _frame=None) -> None:
    global RUNNING
    RUNNING = False


def fit(value: str) -> bytes:
    return f"{value:<{WIDTH}.{WIDTH}s}".encode("ascii", "replace")


@dataclass
class Lcd:
    port: str
    fd: int | None = None

    def open(self) -> None:
        self.fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        attrs = termios.tcgetattr(self.fd)
        attrs[0] = attrs[1] = attrs[3] = 0
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[4] = attrs[5] = termios.B1200
        attrs[6][termios.VMIN] = attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def write(self, data: bytes, delay: float = 0.08) -> None:
        if self.fd is None:
            raise OSError("LCD is closed")
        os.write(self.fd, data)
        time.sleep(delay)

    def initialise(self) -> None:
        self.write(b"\x4d\x0d", 0.05)
        self.write(b"\x4d\x5e\x01", 0.05)

    def lines(self, first: str, second: str) -> None:
        self.write(b"\x4d\x0c\x00\x10" + fit(first))
        self.write(b"\x4d\x0c\x01\x10" + fit(second))

    def off(self) -> None:
        self.lines("", "")
        self.write(b"\x4d\x5e\x00", 0.05)

    def button(self) -> int | None:
        if self.fd is None or not select.select([self.fd], [], [], 0)[0]:
            return None
        data = os.read(self.fd, 64)
        for offset in range(max(0, len(data) - 3)):
            if data[offset:offset + 3] == b"\x53\x05\x00":
                return data[offset + 3]
        return None


def temperatures() -> tuple[float | None, float | None]:
    cpu: list[float] = []
    system: list[float] = []
    for base in glob.glob("/sys/class/hwmon/hwmon*"):
        name = read_text(os.path.join(base, "name"))
        for path in glob.glob(os.path.join(base, "temp*_input")):
            value = valid_temp(path)
            if value is None:
                continue
            if name == "coretemp":
                cpu.append(value)
            elif name in {"qnap8528", "drivetemp", "pch_cometlake", "jc42"}:
                system.append(value)
    return (max(cpu) if cpu else None, max(system) if system else None)


def memory_percent() -> int | None:
    values: dict[str, int] = {}
    for line in read_text("/proc/meminfo").splitlines():
        if ":" in line and line.split()[1].isdigit():
            values[line.split(":", 1)[0]] = int(line.split()[1])
    total, available = values.get("MemTotal"), values.get("MemAvailable")
    if not total or available is None:
        return None
    return round((total - available) * 100 / total)


def fan_summary() -> str:
    values: list[int] = []
    for path in glob.glob("/sys/class/hwmon/hwmon*/fan*_input"):
        raw = read_text(path)
        if raw.isdigit():
            values.append(int(raw))
    if not values:
        return "Fans unavailable"
    return f"Fans {min(values)}-{max(values)}"


def pages() -> list[tuple[str, str]]:
    cpu, system = temperatures()
    ram = memory_percent()
    cpu_text = "n/a" if cpu is None else f"{cpu:.0f}C"
    system_text = "n/a" if system is None else f"{system:.0f}C"
    ram_text = "n/a" if ram is None else f"{ram}%"
    return [
        ("Proxmox VE", time.strftime("%H:%M %d-%m-%y")),
        (f"CPU {cpu_text}", f"System {system_text}"),
        (f"RAM {ram_text}", fan_summary()),
    ]


def run(config_path: str) -> int:
    config = read_config(config_path)
    port = config.get("port", DEFAULT_PORT)
    page_seconds = max(2.0, parse_float(config.get("page_seconds", "6"), 6.0))
    timeout = max(0.0, parse_float(config.get("backlight_timeout_seconds", "300"), 300.0))
    lcd = Lcd(port)
    lcd.open()
    lcd.initialise()
    page = 0
    last_page = 0.0
    last_activity = time.monotonic()
    backlight = True
    try:
        while RUNNING:
            now = time.monotonic()
            button = lcd.button()
            if button is not None:
                # 0x01 is upper/confirm; 0x02 is lower/next on tested hardware.
                last_activity = now
                if not backlight:
                    lcd.write(b"\x4d\x5e\x01", 0.05)
                    backlight = True
                if button in {0x01, 0x02}:
                    page += 1
                    last_page = 0.0
            if timeout and backlight and now - last_activity >= timeout:
                lcd.write(b"\x4d\x5e\x00", 0.05)
                backlight = False
            if backlight and now - last_page >= page_seconds:
                current_pages = pages()
                first, second = current_pages[page % len(current_pages)]
                with exclusive_lock(LCD_LOCK):
                    lcd.lines(first, second)
                page += 1
                last_page = now
            time.sleep(0.2)
    finally:
        if config.get("backlight_off_on_stop", "true").lower() in {"1", "true", "yes", "on"}:
            with exclusive_lock(LCD_LOCK):
                lcd.off()
        lcd.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="TVS-1288X LCD status daemon")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--off", action="store_true")
    args = parser.parse_args()
    if not require_supported_model():
        return 4
    config = read_config(args.config)
    if args.off:
        lcd = Lcd(config.get("port", DEFAULT_PORT))
        try:
            lcd.open()
            with exclusive_lock(LCD_LOCK):
                lcd.off()
            return 0
        finally:
            lcd.close()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        return run(args.config)
    except (OSError, TimeoutError) as exc:
        print(f"LCD ERROR: {type(exc).__name__}: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
