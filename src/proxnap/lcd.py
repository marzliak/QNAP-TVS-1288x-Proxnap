#!/usr/bin/env python3
"""
LCD status daemon for QNAP TVS-1288X on Proxmox.

The front LCD is a 16x2 display on /dev/ttyS1 using the QNAP/ICP 0x4D serial
protocol. This daemon keeps the display focused on operational status: Proxmox,
CPU/memory, network, temperatures, fans, ZFS pools/datasets, disks, services
and alerts.

Operator guide:

Physical buttons:
- Upper button: reports 0x01, named OK/Set/Confirm.
- Lower button: reports 0x02, named Next/Select.
- Rule of thumb: the lower button moves forward; the upper button decides.

Landing page:
- The daemon starts on a live landing page instead of the numbered menu.
- Frames rotate every home_frame_seconds:
  1. pressure: load 1/5 min plus RAM usage;
  2. workload: hostname/uptime plus VM/LXC count;
  3. network: aggregate RX/TX throughput.
- Lower press: manually advances to the next landing frame.
- Upper press: opens the numbered menu at "1. Status".
- Upper hold for HOME_LOCK_SECONDS: locks/unlocks the current landing frame.
- Locked landing shows "L" in the last column instead of the heartbeat.

Numbered menu:
- Cover pages are "1. Status", "2. Network", "3. Storage", "4. Thermal",
  and "5. Actions".
- Lower press on a cover page advances to the next cover page.
- Upper press on a cover page enters the section at its first detail page.
- Lower press inside a section advances to the next detail page. After the
  last detail page, it moves to the next cover page.
- Upper press on a normal detail page returns to that section cover.
- Upper press on an action detail page opens or executes that action.

Available screens:
- 0. Landing
  - pressure: load and RAM;
  - workload: uptime and VM/LXC count;
  - network: RX/TX Mbps.
- 1. Status
  - 1.1 Proxmox: PVE version, uptime, load;
  - 1.2 CPU/RAM: CPU usage/speed and RAM used/total;
  - 1.x Services: configured daemon health;
  - 1.x Alerts: ZFS/systemd/update alerts, or "None".
- 2. Network
  - one "IP" page for each visible interface with an address;
  - primary interface aggregate current RX/TX;
  - primary interface aggregate RX/TX totals;
  - dynamic per-interface RX/TX and total pages for other visible interfaces.
- 3. Storage
  - 3.1 Pools: online/total pool count and allocated/total size.
- 4. Thermal
  - 4.1 CPU: CPU temperature, usage, speed, load;
  - 4.2 Temps: CPU/NVMe/HDD temperatures;
  - 4.3 System temp: system/board temperature;
  - 4.4 Fans: fan RPM summary and fan mode action.
- 5. Actions
  - 5.1 Updates: opens the update submenu;
  - 5.2 Reboot: opens a protected confirmation screen;
  - 5.3 Shutdown: opens a protected confirmation screen.
- 5.1 Updates submenu
  - 5.1.1 Ref+Run: refresh apt metadata, then install updates;
  - 5.1.2 Refresh: refresh apt metadata/update count only;
  - 5.1.3 Run: install already-known updates without refreshing first.
- 4.4 Fans submenu
  - modes: auto, quiet, normal, extra, full;
  - lower press cycles mode;
  - upper press saves/leaves.

Dangerous actions:
- Reboot/Shutdown never run on a simple OK press.
- Upper press opens confirmation.
- Upper hold for POWER_CONFIRM_SECONDS executes.
- Lower press cancels/goes back.

Idle behavior:
- Menu pages return to landing after MENU_IDLE_SECONDS.
- Landing turns the backlight off after timeout_seconds from the config.
- Any LCD button wakes the display back to the landing page.
"""

import argparse
import fcntl
import glob
import json
import math
import os
import re
import select
import signal
import socket
import stat
import struct
import textwrap
import unicodedata
import subprocess
import termios
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass


LCD_PORT = "/dev/ttyS1"
LCD_WIDTH = 16
DEFAULT_CONFIG = "/etc/proxnap/lcd.conf"
FAN_CONFIG = "/etc/proxnap/fan-control.conf"
FAN_CONTROL = "/usr/local/sbin/proxnap-fan-control"
COPY_UPDATE_CONFIG = "/etc/proxnap/copy-button.conf"
COPY_UPDATE_CONTROL = "/usr/local/sbin/proxnap-copy-update"
STATUS_STATE_FILE = "/run/proxnap-status.json"
LCD_MESSAGE_LOCK = "/run/proxnap-lcd-message.lock"
HOME_LOCK_SECONDS = 1.5
MENU_IDLE_SECONDS = 120.0
POWER_CONFIRM_SECONDS = 2.0
BUTTON_MIN_PRESS_SECONDS = 0.0
BUTTON_DEBOUNCE_SECONDS = 0.20
FAN_MODES = ("auto", "quiet", "normal", "extra", "full")

running = True
last_update_check = 0.0
last_update_count: int | None = None
last_pve_check = 0.0
last_pve_value = "PVE?"


@dataclass
class Settings:
    enabled: bool = True
    page_seconds: float = 8.0
    home_frame_seconds: float = 7.0
    home_tick_seconds: float = 0.5
    heartbeat_enabled: bool = True
    timeout_seconds: int = 300
    config_reload_seconds: int = 30
    data_refresh_seconds: int = 15
    primary_net: str = "auto"
    interface_exclude_prefixes: tuple[str, ...] = (
        "lo",
        "tap",
        "veth",
        "fwbr",
        "fwln",
        "fwpr",
        "docker",
        "br-",
    )
    dataset_include: tuple[str, ...] = ("auto",)
    dataset_warn_percent: int = 85
    services: tuple[str, ...] = ("pveproxy", "pvedaemon", "pvestatd")
    button_input: str = "/dev/input/by-path/platform-qnap8528-event"
    lcd_message_lock: str = LCD_MESSAGE_LOCK
    power_status_enabled: bool = False
    power_status_inputs: tuple[str, ...] = ()
    power_status_page_seconds: float = 3.0


@dataclass
class NetSnapshot:
    ts: float
    rx: dict[str, int]
    tx: dict[str, int]


@dataclass
class LcdPage:
    group: str
    title: str
    line1: str
    line2: str
    action: str | None = None


@dataclass
class LiveSnapshot:
    hostname: str = "host"
    load_1: float | None = None
    load_5: float | None = None
    ram_used_gb: float | None = None
    ram_total_gb: float | None = None
    ram_pct: float | None = None
    vm_count: int | None = None
    lxc_count: int | None = None
    uptime_short: str | None = None
    net_rx_bps: float | None = None
    net_tx_bps: float | None = None


GROUPS = ("Status", "Network", "Storage", "Thermal", "Actions")


def stop(_signum=None, _frame=None):
    global running
    running = False


def read_file(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def sh(args: list[str], timeout: int = 8) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            args,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip()
    except Exception as exc:
        return 127, str(exc)


def read_config(path: str) -> dict[str, str]:
    config: dict[str, str] = {}
    for line in read_file(path).splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, value = [part.strip() for part in line.split("=", 1)]
        config[key] = value
    return config


def parse_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def parse_float(value: str, default: float) -> float:
    try:
        return float(value.strip())
    except ValueError:
        return default


def parse_int(value: str, default: int) -> int:
    try:
        return int(value.strip())
    except ValueError:
        return default


def parse_bool(value: str, default: bool) -> bool:
    value = value.strip().lower()
    if value in {"1", "yes", "true", "on"}:
        return True
    if value in {"0", "no", "false", "off"}:
        return False
    return default


def load_settings(path: str) -> Settings:
    raw = read_config(path)
    settings = Settings()
    settings.enabled = parse_bool(raw.get("enabled", "true"), True)
    settings.page_seconds = max(1.0, parse_float(raw.get("page_seconds", "8"), 8.0))
    settings.home_frame_seconds = max(2.0, parse_float(raw.get("home_frame_seconds", "7"), 7.0))
    settings.home_tick_seconds = max(0.25, parse_float(raw.get("home_tick_seconds", "0.5"), 0.5))
    settings.heartbeat_enabled = parse_bool(raw.get("heartbeat_enabled", "true"), True)
    settings.timeout_seconds = max(30, parse_int(raw.get("timeout_seconds", "300"), 300))
    settings.config_reload_seconds = max(5, parse_int(raw.get("config_reload_seconds", "30"), 30))
    settings.data_refresh_seconds = max(2, parse_int(raw.get("data_refresh_seconds", "15"), 15))
    settings.primary_net = raw.get("primary_net", settings.primary_net)
    if "interface_exclude_prefixes" in raw:
        settings.interface_exclude_prefixes = parse_csv(raw["interface_exclude_prefixes"])
    if "dataset_include" in raw:
        settings.dataset_include = parse_csv(raw["dataset_include"])
    settings.dataset_warn_percent = max(1, min(99, parse_int(raw.get("dataset_warn_percent", "85"), 85)))
    if "services" in raw:
        settings.services = parse_csv(raw["services"])
    settings.button_input = raw.get("button_input", settings.button_input)
    settings.lcd_message_lock = raw.get("lcd_message_lock", settings.lcd_message_lock)
    settings.power_status_enabled = parse_bool(raw.get("power_status_enabled", "false"), False)
    # Explicit absolute paths only; never discover input devices implicitly.
    settings.power_status_inputs = tuple(dict.fromkeys(
        path for path in parse_csv(raw.get("power_status_inputs", ""))
        if os.path.isabs(path) and len(path) <= 4096
    ))[:8]
    seconds = parse_float(raw.get("power_status_page_seconds", "3.0"), 3.0)
    settings.power_status_page_seconds = seconds if math.isfinite(seconds) and 1 <= seconds <= 10 else 3.0
    return settings


@contextmanager
def lcd_transaction(path: str):
    """Try ownership once; keep it through all writes and their UART delays.

    Empty/invalid paths and acquisition errors deny output, just like contention.
    Closing the independent descriptor releases flock even if a write fails.
    """
    lock = None
    try:
        if not path:
            yield False
            return
        try:
            lock = open(path, "a", encoding="utf-8")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ValueError):
            if lock is not None:
                lock.close()
                lock = None
            yield False
            return
        yield True
    finally:
        if lock is not None:
            lock.close()


def fit(text: str, width: int = LCD_WIDTH) -> str:
    clean = " ".join(str(text).replace("\t", " ").split())
    return f"{clean:<{width}.{width}s}"


def add_section_prefix(line1: str, group_index: int, page_index: int) -> str:
    return f"{group_index + 1}.{page_index + 1} {line1}"


def page(group: str, title: str, line1: str, line2: str, action: str | None = None) -> LcdPage:
    return LcdPage(group, title, line1, line2, action)


def fmt_bytes(value: float, suffix: str = "B") -> str:
    units = ["", "K", "M", "G", "T", "P"]
    v = float(max(0.0, value))
    unit = units[0]
    for unit in units:
        if v < 999.5 or unit == units[-1]:
            break
        v /= 1024.0
    if v >= 100 or unit == "":
        num = f"{v:.0f}"
    elif v >= 10:
        num = f"{v:.1f}"
    else:
        num = f"{v:.1f}"
    if unit == "" and suffix.startswith("/"):
        return f"{num}B{suffix}"
    return f"{num}{unit}{suffix}"


def fmt_rate(value: float) -> str:
    return fmt_bytes(value, "/s")


def fmt_rate_lcd(value: float) -> str:
    return fmt_bytes(value, "")


def fmt_mem_pair(used_bytes: float, total_bytes: float) -> str:
    used_g = used_bytes / (1024 ** 3)
    total_g = total_bytes / (1024 ** 3)
    if total_g >= 100:
        return f"{used_g:.1f}/{total_g:.0f}G"
    if total_g >= 10:
        return f"{used_g:.1f}/{total_g:.1f}G"
    return f"{used_g:.1f}/{total_g:.1f}G"


def fmt_percent(value: float) -> str:
    return f"{round(value):.0f}%"


def uptime_short() -> str:
    raw = read_file("/proc/uptime").split()[0]
    seconds = int(float(raw)) if raw else 0
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"up{days}d{hours}h"
    if hours:
        return f"up{hours}h{minutes}m"
    return f"up{minutes}m"


def uptime_value_short() -> str:
    return uptime_short().removeprefix("up")


def parse_loads() -> tuple[float, float]:
    parts = read_file("/proc/loadavg").split()
    try:
        return float(parts[0]), float(parts[1])
    except (IndexError, ValueError):
        return 0.0, 0.0


def heartbeat_char(
    now_mono: float,
    period_seconds: float = 1.0,
    on_fraction: float = 0.3,
    on_char: str = "*",
    off_char: str = " ",
) -> str:
    phase = (now_mono % period_seconds) / period_seconds
    return on_char if phase < on_fraction else off_char


def ascii_safe(text: str) -> str:
    return "".join(c if 0x20 <= ord(c) <= 0x7E else "?" for c in str(text))


def to_lcd(text: str) -> str:
    return ascii_safe(text).ljust(LCD_WIDTH)[:LCD_WIDTH]


def fmt_home_load(value: float | None) -> str:
    value = value or 0.0
    if value < 10:
        return f"{value:>4.2f}"
    if value < 100:
        return f"{value:>4.1f}"
    return f"{value:>4.0f}"


def bps_to_mbps(value: float | None) -> int:
    if value is None or value < 0:
        return 0
    return int(round(value * 8.0 / 1_000_000))


def ram_pct(snapshot: LiveSnapshot) -> int:
    if snapshot.ram_pct is not None:
        return max(0, min(100, int(round(snapshot.ram_pct))))
    if snapshot.ram_used_gb is not None and snapshot.ram_total_gb:
        pct = 100.0 * snapshot.ram_used_gb / snapshot.ram_total_gb
        return max(0, min(100, int(round(pct))))
    return 0


def vm_lxc_counts() -> tuple[int, int]:
    vm_count = 0
    lxc_count = 0
    rc, out = sh(["qm", "list"], timeout=5)
    if rc == 0:
        vm_count = sum(1 for line in out.splitlines()[1:] if line.strip())
    rc, out = sh(["pct", "list"], timeout=5)
    if rc == 0:
        lxc_count = sum(1 for line in out.splitlines()[1:] if line.strip())
    return vm_count, lxc_count


def home_frame_index(now: float, settings: Settings, locked_frame: int | None = None) -> int:
    if locked_frame is not None:
        return locked_frame % 3
    return int(now / settings.home_frame_seconds) % 3


def home_lines(
    snapshot: LiveSnapshot | None,
    now: float,
    settings: Settings,
    locked_frame: int | None = None,
    manual_frame: int | None = None,
) -> tuple[str, str, int]:
    hb = "L" if locked_frame is not None else heartbeat_char(now) if settings.heartbeat_enabled else " "
    frame = home_frame_index(now, settings, locked_frame if locked_frame is not None else manual_frame)

    if snapshot is None:
        return to_lcd("LCD data unknown"), to_lcd("Waiting for data"), frame

    if frame == 0:
        used = int(round(snapshot.ram_used_gb or 0.0))
        total = int(round(snapshot.ram_total_gb or 0.0))
        line1 = f"LOAD {fmt_home_load(snapshot.load_1)}/{fmt_home_load(snapshot.load_5)}"
        line2 = f"RAM {ram_pct(snapshot)}% {used}/{total}".ljust(15)[:15] + hb
        return to_lcd(line1), to_lcd(line2), frame

    if frame == 1:
        host = (snapshot.hostname or "host").upper()[:6]
        vm = snapshot.vm_count if snapshot.vm_count is not None else 0
        lxc = snapshot.lxc_count if snapshot.lxc_count is not None else 0
        line1 = f"{host} up {snapshot.uptime_short or '?'}"
        line2 = f"VMs {vm:>2} LXC {lxc:>2}".ljust(15)[:15] + hb
        return to_lcd(line1), to_lcd(line2), frame

    rx = bps_to_mbps(snapshot.net_rx_bps)
    tx = bps_to_mbps(snapshot.net_tx_bps)
    line1 = f"RX{rx:>5} Mbps"
    line2 = f"TX{tx:>5} Mbps".ljust(15)[:15] + hb
    return to_lcd(line1), to_lcd(line2), frame


def pve_short() -> str:
    global last_pve_check, last_pve_value
    now = time.monotonic()
    if last_pve_check and now - last_pve_check < 3600:
        return last_pve_value
    rc, out = sh(["pveversion"], timeout=5)
    match = re.search(r"pve-manager/([0-9.]+)", out)
    if not match:
        return last_pve_value
    major_minor = ".".join(match.group(1).split(".")[:2])
    last_pve_value = f"Proxmox {major_minor}"
    last_pve_check = now
    return last_pve_value


def load_short() -> str:
    parts = read_file("/proc/loadavg").split()
    if len(parts) >= 2:
        return f"Load {parts[0]} {parts[1]}"
    return "Load ?"


def load_compact() -> str:
    parts = read_file("/proc/loadavg").split()
    if len(parts) >= 2:
        try:
            return f"L{float(parts[0]):.1f}/{float(parts[1]):.1f}"
        except ValueError:
            pass
    return "L?"


def read_mem() -> tuple[int, int, int, int]:
    total = available = swap_total = swap_free = 0
    for line in read_file("/proc/meminfo").splitlines():
        key, value = line.split(":", 1)
        kb = int(value.split()[0])
        if key == "MemTotal":
            total = kb
        elif key == "MemAvailable":
            available = kb
        elif key == "SwapTotal":
            swap_total = kb
        elif key == "SwapFree":
            swap_free = kb
    used = max(0, total - available)
    swap_used = max(0, swap_total - swap_free)
    return used, total, swap_used, swap_total


def cpu_stat() -> tuple[int, int]:
    parts = read_file("/proc/stat").splitlines()[0].split()[1:]
    values = [int(v) for v in parts]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return idle, total


def cpu_usage(prev: tuple[int, int] | None, cur: tuple[int, int]) -> float:
    if not prev:
        return 0.0
    idle_delta = cur[0] - prev[0]
    total_delta = cur[1] - prev[1]
    if total_delta <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * (1.0 - idle_delta / total_delta)))


def cpu_freq_ghz() -> float:
    freqs = []
    for path in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq"):
        raw = read_file(path)
        if raw.isdigit():
            freqs.append(int(raw))
    if freqs:
        return max(freqs) / 1_000_000.0
    mhz = []
    for line in read_file("/proc/cpuinfo").splitlines():
        if line.startswith("cpu MHz"):
            try:
                mhz.append(float(line.split(":", 1)[1]))
            except ValueError:
                pass
    return max(mhz) / 1000.0 if mhz else 0.0


def hwmon_by_name(name: str) -> list[str]:
    paths = []
    for path in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        if read_file(os.path.join(path, "name")) == name:
            paths.append(path)
    return paths


def read_temp(path: str) -> float | None:
    raw = read_file(path)
    if not raw.lstrip("-").isdigit():
        return None
    value = int(raw)
    if 0 < value < 130000:
        return value / 1000.0
    return None


def coretemp_package() -> float | None:
    for hwmon in hwmon_by_name("coretemp"):
        for label in glob.glob(os.path.join(hwmon, "temp*_label")):
            if read_file(label) == "Package id 0":
                return read_temp(label.replace("_label", "_input"))
    return None


def max_temp_for_names(names: tuple[str, ...]) -> float | None:
    temps = []
    for name in names:
        for hwmon in hwmon_by_name(name):
            for path in glob.glob(os.path.join(hwmon, "temp*_input")):
                temp = read_temp(path)
                if temp is not None:
                    temps.append(temp)
    return max(temps) if temps else None


def qnap_hwmon() -> str | None:
    paths = hwmon_by_name("qnap8528")
    return paths[0] if paths else None


def fan_summary() -> tuple[str, str]:
    mode = read_config(FAN_CONFIG).get("mode", "auto")[:6]
    # Do not poll qnap8528 fan RPM/PWM from the LCD refresh path. On the reference chassis,
    # repeated EC reads can produce short audible rear-fan pulses.
    return f"Fans {mode}", "managed quiet"


def ip_pages(settings: Settings) -> list[tuple[str, str]]:
    pages = []
    rc, out = sh(["ip", "-o", "-4", "addr", "show", "scope", "global"])
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            iface = parts[1]
            ip = parts[3].split("/", 1)[0]
            if not interface_visible(iface, settings):
                continue
            pages.append((iface, ip))
    return pages or [("IP", "none")]


def all_net_stats() -> NetSnapshot:
    rx = {}
    tx = {}
    for iface in os.listdir("/sys/class/net"):
        base = f"/sys/class/net/{iface}/statistics"
        r = read_file(os.path.join(base, "rx_bytes"))
        t = read_file(os.path.join(base, "tx_bytes"))
        if r.isdigit() and t.isdigit():
            rx[iface] = int(r)
            tx[iface] = int(t)
    return NetSnapshot(time.monotonic(), rx, tx)


def interface_visible(iface: str, settings: Settings) -> bool:
    return not any(iface.startswith(prefix) for prefix in settings.interface_exclude_prefixes)


def link_up(iface: str) -> bool:
    return read_file(f"/sys/class/net/{iface}/operstate") in {"up", "unknown"}


def visible_interfaces(settings: Settings) -> list[str]:
    ifaces = []
    for iface in sorted(os.listdir("/sys/class/net")):
        if interface_visible(iface, settings) and link_up(iface):
            ifaces.append(iface)
    return ifaces


def primary_interfaces(settings: Settings, visible: list[str]) -> list[str]:
    if settings.primary_net != "auto":
        return [i for i in parse_csv(settings.primary_net) if i in visible]
    bonds = [i for i in visible if i.startswith("bond")]
    if bonds:
        return bonds
    if "vmbr0" in visible:
        return ["vmbr0"]
    bridges = [i for i in visible if i.startswith("vmbr")]
    return bridges[:1] or visible


def net_line(ifaces: list[str], prev: NetSnapshot, cur: NetSnapshot) -> tuple[float, float, int, int]:
    elapsed = max(0.1, cur.ts - prev.ts)
    rx_now = sum(cur.rx.get(i, 0) for i in ifaces)
    tx_now = sum(cur.tx.get(i, 0) for i in ifaces)
    rx_prev = sum(prev.rx.get(i, 0) for i in ifaces)
    tx_prev = sum(prev.tx.get(i, 0) for i in ifaces)
    return (rx_now - rx_prev) / elapsed, (tx_now - tx_prev) / elapsed, rx_now, tx_now


def zpool_rows() -> list[tuple[str, int, int, str]]:
    rc, out = sh(["zpool", "list", "-Hp", "-o", "name,size,alloc,health"])
    rows = []
    if rc != 0:
        return rows
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            try:
                rows.append((parts[0], int(parts[1]), int(parts[2]), parts[3]))
            except ValueError:
                pass
    return rows


def zfs_dataset_rows(settings: Settings) -> list[tuple[str, int, int, str, str]]:
    rc, out = sh(["zfs", "list", "-Hp", "-o", "name,used,avail,mountpoint"])
    rows = []
    if rc != 0:
        return rows
    include = settings.dataset_include
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        name, used_s, avail_s, mountpoint = parts[:4]
        if include != ("auto",) and not any(name.startswith(prefix) for prefix in include):
            continue
        if include == ("auto",) and "/" not in name:
            continue
        try:
            used = int(used_s)
            avail = int(avail_s)
        except ValueError:
            continue
        rows.append((name, used, avail, mountpoint, name.split("/", 1)[0]))
    return rows[:16]


def mounted(path: str) -> bool:
    if path in {"-", "legacy", "none"}:
        return True
    for line in read_file("/proc/mounts").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == path:
            return True
    return False


def disk_summary() -> tuple[str, str]:
    rc, out = sh(["lsblk", "-dn", "-o", "NAME,TYPE"])
    disks = [line.split()[0] for line in out.splitlines() if line.split()[1:] == ["disk"]]
    _smart_bad = 0
    configured = 0
    for line in read_file("/etc/proxnap/disk-leds.conf").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = [part.strip() for part in line.split("=", 1)]
        if key == "panel_brightness":
            continue
        dev = os.path.realpath(value)
        if os.path.exists(dev):
            configured += 1
    return f"Disks {len(disks)}/14", f"Mapped {configured}"


def services_page(settings: Settings) -> tuple[str, str]:
    states = []
    for svc in settings.services:
        rc, out = sh(["systemctl", "is-active", svc], timeout=3)
        if rc == 0:
            states.append(f"{svc}:OK")
        else:
            states.append(f"{svc}:--")
    ok = sum(1 for s in states if s.endswith(":OK"))
    down = " ".join(s.split(":", 1)[0][:3] for s in states if s.endswith(":--"))
    return f"Services {ok}/{len(settings.services)}", f"Down {down}" if down else "All active"


def apt_update_count() -> int | None:
    global last_update_check, last_update_count
    now = time.monotonic()
    if last_update_count is not None and now - last_update_check < 21600:
        return last_update_count

    rc, out = sh(["apt", "list", "--upgradable"], timeout=30)
    if rc != 0:
        return last_update_count
    upgrades = [
        line
        for line in out.splitlines()
        if line.strip() and not line.startswith("Listing") and "/" in line
    ]
    last_update_check = now
    last_update_count = len(upgrades)
    return last_update_count


def short_status_reason(reason: str) -> str:
    reason = reason.replace("failed systemd units: ", "systemd ")
    reason = reason.replace("temperature ", "temp ")
    reason = reason.replace("root filesystem is read-only", "rootfs read-only")
    reason = reason.replace("qnap8528 module missing", "qnap8528 missing")
    return reason[:LCD_WIDTH]


def critical_status_page() -> tuple[str, str]:
    unknown = ("System Check", "Status unknown")
    try:
        with open(STATUS_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            return unknown
        stamp = state.get("timestamp")
        period = state.get("check_seconds", 30)
        if type(stamp) is not int or stamp <= 0:
            return unknown
        if type(period) not in {int, float} or not 0 < period < float("inf"):
            return unknown
        reasons, warnings = state.get("reasons"), state.get("warnings")
        unavailable = state.get("unavailable", [])
        if not all(isinstance(items, list) and all(isinstance(x, str) for x in items)
                   for items in (reasons, warnings, unavailable)):
            return unknown
        if not 0 <= time.time() - stamp <= period:
            return "System Check", "Status stale"
        status = state.get("status")
        if status == "critical" and reasons:
            return "Critical", short_status_reason(reasons[0])
        if unavailable:
            return "System Check", "Data unavailable"
        if status == "warning" and warnings and not reasons:
            return "Warning", short_status_reason(warnings[0])
        if status == "ok" and not reasons and not warnings:
            return "System Check", "All running OK"
    except (OSError, ValueError, TypeError):
        pass
    return unknown


def alert_pages(settings: Settings) -> list[tuple[str, str]]:
    alerts = [critical_status_page()]
    rc, out = sh(["systemctl", "--failed", "--no-legend", "--plain"], timeout=5)
    failed = [line.split()[0] for line in out.splitlines() if line.strip()]
    if failed:
        alerts.append(("Alert systemd", failed[0][:16]))
    for name, _size, _alloc, health in zpool_rows():
        if health != "ONLINE":
            alerts.append((f"Alert {name}", health))
    cpu = coretemp_package()
    if cpu and cpu >= 85:
        alerts.append(("Alert CPU temp", f"{cpu:.0f}C"))
    return alerts


def _power_input_identity(fd: int, resolved: str) -> int:
    """Validate the opened FD, not just its configurable alias. Read-only metadata.

    No grab or clock-setting ioctl: evdev's default timestamps are CLOCK_REALTIME.
    Sysfs capability words are native unsigned-long words, most significant first.
    """
    info = os.fstat(fd)
    actual = os.path.realpath(f"/proc/self/fd/{fd}")
    if not stat.S_ISCHR(info.st_mode) or actual != resolved or not re.fullmatch(r"/dev/input/event[0-9]+", actual):
        raise ValueError("not an input event character device")
    base = f"/sys/class/input/{os.path.basename(actual)}"
    dev = read_file(f"{base}/dev")
    if dev != f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}":
        raise ValueError("input device identity changed")
    if "".join(read_file(f"{base}/device/name").split()) != "PowerButton":
        raise ValueError("not a Power Button")
    words = read_file(f"{base}/device/capabilities/key").split()
    word_bits = struct.calcsize("L") * 8
    if not words or len(words) > 64:
        raise ValueError("invalid key capabilities")
    bits = 0
    for word in words:
        value = int(word, 16)
        if not 0 <= value < (1 << word_bits):
            raise ValueError("invalid capability word")
        bits = (bits << word_bits) | value
    if not bits & (1 << 116):  # KEY_POWER, never an LCD navigation mask
        raise ValueError("KEY_POWER absent")
    return info.st_rdev


class _PowerStatusInputs:
    """Bounded nonblocking evdev reader, owned exclusively by the LCD loop."""

    EVENT = struct.Struct("llHHi")

    def __init__(self):
        self.sources: dict[str, dict] = {}
        self.last_press = -float("inf")
        self.discard_before = 0.0

    def _disconnect(self, source: dict, mono: float):
        fd = source.get("fd")
        source.update(fd=None, buffer=b"", held=False, retry=mono + 2.0)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def close(self):
        for source in self.sources.values():
            self._disconnect(source, 0.0)
        self.sources.clear()

    def configure(self, paths: tuple[str, ...], mono: float):
        wanted = tuple(dict.fromkeys(p for p in paths if os.path.isabs(p) and len(p) <= 4096))[:8]
        for path in list(self.sources):
            if path not in wanted:
                self._disconnect(self.sources.pop(path), mono)
        for path in wanted:
            self.sources.setdefault(path, dict(fd=None, buffer=b"", held=False, retry=0.0))

    def poll(self, mono: float, accept: bool) -> bool:
        requested = False
        for path, source in self.sources.items():
            if source["fd"] is None and mono >= source["retry"]:
                fd = None
                try:
                    resolved = os.path.realpath(path)
                    if not re.fullmatch(r"/dev/input/event[0-9]+", resolved):
                        raise ValueError("not an event path")
                    opened = time.time()
                    fd = os.open(resolved, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW)
                    identity = _power_input_identity(fd, resolved)
                    if any(s.get("identity") == identity and s["fd"] is not None for s in self.sources.values()):
                        raise ValueError("duplicate device alias")
                    source.update(fd=fd, identity=identity, opened=opened, buffer=b"", held=False, sync_lost=False)
                    fd = None
                except (OSError, ValueError):
                    source["retry"] = mono + 2.0
                finally:
                    if fd is not None:
                        os.close(fd)
            if source["fd"] is None:
                continue
            # Bound CPU per poll even if an input is continuously noisy.
            for _ in range(4):
                try:
                    chunk = os.read(source["fd"], self.EVENT.size * 32)
                    if not chunk:
                        self._disconnect(source, mono)
                        break
                except BlockingIOError:
                    break
                except OSError:
                    self._disconnect(source, mono)
                    break
                wall = time.time()
                data = source["buffer"] + chunk
                end = len(data) - len(data) % self.EVENT.size
                source["buffer"] = data[end:]
                for offset in range(0, end, self.EVENT.size):
                    sec, usec, kind, code, value = self.EVENT.unpack_from(data, offset)
                    if kind == 0 and code == 3:  # SYN_DROPPED: require sync then release
                        source["held"] = True
                        source["sync_lost"] = True
                    if source["sync_lost"]:
                        if kind == 0 and code == 0:  # SYN_REPORT
                            source["sync_lost"] = False
                        continue
                    if kind != 1 or code != 116:
                        continue
                    if value == 0:
                        source["held"] = False
                        continue
                    held = source["held"]
                    source["held"] = True
                    stamp = sec + usec / 1_000_000.0
                    if (value != 1 or held or not 0 <= usec < 1_000_000
                            or not 0 <= wall - stamp <= 1.0
                            or stamp < source["opened"] or stamp <= self.discard_before):
                        continue
                    # One physical actuation can arrive from both ACPI devices.
                    # Keep the newest timestamp independent of FD iteration order.
                    duplicate = abs(stamp - self.last_press) < 0.35
                    if stamp > self.last_press:
                        self.last_press = stamp
                    if accept and not duplicate:
                        requested = True
            if not accept:
                # A fragmented or over-budget queued frame must not complete later
                # as a fresh request after an external writer releases ownership.
                self.discard_before = max(self.discard_before, time.time())
        return requested


def _power_status_pages() -> list[tuple[str, str]]:
    """Current indicator snapshot, bounded to 64K characters and 32 2x16 pages."""
    unknown = [("Sem dados", "Desconhecido")]
    try:
        with open(STATUS_STATE_FILE, "r", encoding="utf-8") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            return unknown
        state = json.loads(raw)
        if not isinstance(state, dict):
            return unknown
        stamp, period = state.get("timestamp"), state.get("check_seconds", 30)
        if type(stamp) is not int or stamp <= 0:
            return unknown
        if type(period) not in {int, float} or not 0 < period < float("inf"):
            return unknown
        if not 0 <= time.time() - stamp <= period:
            return unknown
        reasons, warnings, unavailable = (state.get("reasons"), state.get("warnings"), state.get("unavailable", []))
        if not all(isinstance(items, list) and all(isinstance(x, str) and x.strip() for x in items)
                   for items in (reasons, warnings, unavailable)):
            return unknown
        status = state.get("status")
        if status == "critical" and reasons:
            title, messages = "Critico", reasons
        elif unavailable:
            return unknown
        elif status == "warning" and warnings and not reasons:
            title, messages = "Aviso", warnings
        elif status == "ok" and not reasons and not warnings:
            return [("Estado atual", "Tudo OK")]
        else:
            return unknown
        lines = []
        for message in messages:
            normalized = unicodedata.normalize("NFKD", message)
            clean = "".join(c for c in normalized if not unicodedata.combining(c))
            clean = clean.encode("ascii", "replace").decode("ascii")
            clean = " ".join("".join(c if 32 <= ord(c) <= 126 else " " for c in clean).split())
            for line in textwrap.wrap(clean, width=LCD_WIDTH):
                lines.append(line)
                if len(lines) > 32:
                    return [(f"{title} {i + 1}/32", text) for i, text in enumerate(lines[:31])] + [(title, "Mais: limite LCD")]
        return [(f"{title} {i + 1}/{len(lines)}", text) for i, text in enumerate(lines)] or unknown
    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
        return unknown


class _PowerStatusPopup:
    """Information only. Freeze safe navigation; never replay administrative UI."""

    def __init__(self, ui, now: float, seconds: float):
        self.pages = _power_status_pages()
        self.started = now
        self.seconds = seconds if math.isfinite(seconds) and 1 <= seconds <= 10 else 3.0
        self.manual_remaining = max(0.0, ui.manual_until - now)
        if ui.mode not in {"home", "cover", "section"}:
            ui.go_home(now)
            self.manual_remaining = 0.0
        ui.confirm_action = None
        ui.override = None
        ui.override_until = 0.0
        ui.reset_press()

    def lines(self, now: float) -> tuple[str, str] | None:
        index = int(max(0.0, now - self.started) / self.seconds)
        return self.pages[index] if index < len(self.pages) else None

    def finish(self, ui, now: float):
        ui.reset_press()
        ui.manual_until = now + self.manual_remaining if self.manual_remaining else 0.0


class InputButtons:
    EVENT = struct.Struct("llHHi")
    BTN_0 = 0x100
    BTN_1 = 0x101
    BTN_2 = 0x102

    def __init__(self, path: str):
        self.path = path
        self.fd: int | None = None

    def open(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            self.fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            self.fd = None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def read_buttons(self) -> list[int]:
        if self.fd is None:
            return []
        try:
            data = os.read(self.fd, self.EVENT.size * 16)
        except BlockingIOError:
            return []
        except OSError:
            self.close()
            return []

        buttons = []
        for offset in range(0, len(data) - self.EVENT.size + 1, self.EVENT.size):
            _sec, _usec, event_type, code, value = self.EVENT.unpack_from(data, offset)
            if event_type != 1:
                continue
            mask = 0
            if code == self.BTN_0:
                mask = 0x01
            elif code == self.BTN_1:
                mask = 0x02
            elif code == self.BTN_2:
                mask = 0x03
            if mask and value == 0:
                mask = 0
            if mask or value == 0:
                buttons.append(mask)
        return buttons


class Lcd:
    def __init__(self, port: str):
        self.port = port
        self.fd: int | None = None
        self.backlight_on = False
        self._button_buffer = b""

    def open(self):
        self._button_buffer = b""
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
        self._button_buffer = b""
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _write(self, data: bytes, delay: float = 0.12):
        if self.fd is None:
            return
        os.write(self.fd, data)
        time.sleep(delay)

    def clear(self):
        self._write(b"\x4d\x28", 0.18)
        self.write_lines("", "")

    def backlight(self, on: bool):
        self._write(b"\x4d\x5e" + bytes([1 if on else 0]), 0.05)
        self.backlight_on = on

    def stop_clock(self):
        self._write(b"\x4d\x0d", 0.05)

    def write_lines(self, line1: str, line2: str):
        l1 = fit(line1).encode("ascii", "replace")
        l2 = fit(line2).encode("ascii", "replace")
        self._write(b"\x4d\x0c\x00\x10" + l1)
        self._write(b"\x4d\x0c\x01\x10" + l2)

    def read_buttons(self) -> list[int]:
        if self.fd is None:
            self._button_buffer = b""
            return []
        try:
            readable, _, _ = select.select([self.fd], [], [], 0)
            if not readable:
                return []
            chunk = os.read(self.fd, 64)
        except BlockingIOError:
            return []
        except OSError:
            self._button_buffer = b""
            raise
        # VMIN=0 may yield an empty read; it is not a connection boundary.
        if not chunk:
            return []
        data = self._button_buffer + chunk
        buttons = []
        for i in range(max(0, len(data) - 3)):
            if data[i : i + 3] == b"\x53\x05\x00":
                buttons.append(data[i + 3])
        # Only these starts lack a fourth byte. Keeping them also preserves
        # the original sliding scan for overlapping headers and arbitrary masks.
        self._button_buffer = data[-3:]
        return buttons


def current_fan_mode() -> str:
    mode = read_config(FAN_CONFIG).get("mode", "auto").strip().lower()
    return mode if mode in FAN_MODES else "auto"


def set_fan_mode(mode: str) -> bool:
    if mode not in FAN_MODES:
        return False
    rc, _out = sh([FAN_CONTROL, "--config", FAN_CONFIG, "--set-mode", mode], timeout=5)
    return rc == 0


def next_fan_mode(step: int = 1) -> str:
    current = current_fan_mode()
    index = FAN_MODES.index(current) if current in FAN_MODES else 0
    return FAN_MODES[(index + step) % len(FAN_MODES)]


def start_systemd_job(unit: str, args: list[str]) -> bool:
    rc, _out = sh(["systemd-run", f"--unit={unit}", "--collect", "--quiet", *args], timeout=8)
    return rc == 0


def button_name(mask: int) -> str:
    if mask == LcdUi.ENTER:
        return "OK"
    if mask == LcdUi.SELECT:
        return "Next"
    if mask == 0:
        return "Release"
    return f"0x{mask:02x}"


class LcdUi:
    # On this LCD, the physical upper button reports 0x01 and the lower
    # button reports 0x02. The UI follows the QNAP muscle memory:
    # lower button is Next/Select, upper button is Set/OK/Confirm.
    SELECT = 0x02
    ENTER = 0x01
    DANGEROUS = {"UpdateRefreshRun", "UpdateRefresh", "UpdateRun", "Reboot", "Shutdown"}
    UPDATE_ITEMS = (
        ("Ref+Run", "Refresh then run", "UpdateRefreshRun"),
        ("Refresh", "Update cache", "UpdateRefresh"),
        ("Run", "No refresh", "UpdateRun"),
    )

    def __init__(self):
        self.mode = "home"
        self.group_index = 0
        self.page_by_group = {group: 0 for group in GROUPS}
        self.override: tuple[str, str] | None = None
        self.override_until = 0.0
        self.confirm_action: str | None = None
        self.update_index = 0
        self.press_mask = 0
        self.press_started = 0.0
        self.long_handled = False
        self.manual_until = 0.0
        self.last_short_mask = 0
        self.last_short_at = 0.0
        self.home_frame_lock: int | None = None
        self.home_visible_frame = 0

    def wake_manual(self, now: float, seconds: float = MENU_IDLE_SECONDS):
        self.manual_until = max(self.manual_until, now + seconds)

    def set_message(self, line1: str, line2: str, now: float, seconds: float = 1.8):
        self.override = (line1, line2)
        self.override_until = now + seconds
        self.wake_manual(now, seconds)

    def current_group(self) -> str:
        return GROUPS[self.group_index % len(GROUPS)]

    def group_step(self, step: int):
        self.group_index = (self.group_index + step) % len(GROUPS)

    def pages_for_group(self, pages: list[LcdPage], group: str | None = None) -> list[LcdPage]:
        wanted = group or self.current_group()
        grouped = [page for page in pages if page.group == wanted]
        return grouped or [LcdPage(wanted, "Empty", wanted, "No pages")]

    def current_group_pages(self, pages: list[LcdPage]) -> list[LcdPage]:
        return self.pages_for_group(pages, self.current_group())

    def page_index(self, pages: list[LcdPage]) -> int:
        group = self.current_group()
        count = max(1, len(self.pages_for_group(pages, group)))
        index = self.page_by_group.get(group, 0) % count
        self.page_by_group[group] = index
        return index

    def current_page(self, pages: list[LcdPage]) -> LcdPage:
        group_pages = self.current_group_pages(pages)
        return group_pages[self.page_index(pages) % len(group_pages)]

    def cover_summary(self, pages: list[LcdPage]) -> str:
        group = self.current_group()
        group_pages = self.pages_for_group(pages, group)
        preferred = {
            "Status": ("CPU/RAM", "Updates", "Services", "Proxmox"),
            "Network": ("IP", "Net", "Total", "Iface"),
            "Storage": ("Pools", "Disks", "Pool"),
            "Thermal": ("CPU", "Fans", "Temps"),
            "Actions": ("Update", "Reboot", "Shutdown"),
        }.get(group, ())
        for wanted in preferred:
            for item in group_pages:
                if item.title.startswith(wanted) or item.line1.startswith(wanted):
                    if group == "Network" and wanted == "IP":
                        return item.line2
                    return item.line1
        return group_pages[0].line1 if group_pages else "No pages"

    def fan_lines(self) -> tuple[str, str]:
        return "Fan mode", current_fan_mode()

    def update_lines(self) -> tuple[str, str]:
        label, _detail, _action = self.UPDATE_ITEMS[self.update_index % len(self.UPDATE_ITEMS)]
        return f"5.1.{self.update_index + 1} {label}", "OK"

    def action_label(self, action: str) -> str:
        return {
            "UpdateRefreshRun": "5.1.1 Ref+Run",
            "UpdateRefresh": "5.1.2 Refresh",
            "UpdateRun": "5.1.3 Run only",
            "Reboot": "5.2 Reboot",
            "Shutdown": "5.3 Shutdown",
        }.get(action, action)

    def confirm_seconds(self) -> float:
        if self.confirm_action in {"Reboot", "Shutdown"}:
            return POWER_CONFIRM_SECONDS
        return 0.0

    def confirm_lines(self) -> tuple[str, str]:
        if not self.confirm_action:
            self.mode = "section"
            return "Action", "Cancelled"
        if self.confirm_action in {"Reboot", "Shutdown"}:
            return self.action_label(self.confirm_action), f"Hold OK {self.confirm_seconds():.0f}s"
        return self.action_label(self.confirm_action), "OK"

    def reset_press(self):
        self.press_mask = 0
        self.press_started = 0.0
        self.long_handled = False

    def page_step(self, step: int, pages_count: int):
        group = self.current_group()
        self.page_by_group[group] = (self.page_by_group.get(group, 0) + step) % max(1, pages_count)

    def next_page_or_group(self, pages_count: int):
        group = self.current_group()
        index = self.page_by_group.get(group, 0) % max(1, pages_count)
        if index + 1 >= pages_count:
            self.mode = "cover"
            self.group_step(1)
            print(f"ui next group={self.current_group()} from={group}", flush=True)
            return
        self.page_by_group[group] = index + 1
        print(f"ui next page={self.page_by_group[group] + 1} group={group}", flush=True)

    def back(self, now: float):
        if self.mode == "home":
            return
        if self.mode == "cover":
            self.go_home(now)
            return
        if self.mode == "section":
            self.leave_section(now)
            return
        if self.mode in {"fan", "updates", "confirm"}:
            old_mode = self.mode
            self.mode = "section"
            self.confirm_action = None
            print(f"ui back from={old_mode}", flush=True)
            self.set_message("Back", self.current_group(), now)

    def enter_or_return(self, now: float, pages: list[LcdPage]):
        if self.mode == "home":
            self.enter_menu(now)
        elif self.mode == "cover":
            self.enter_cover(now)
        elif self.mode == "section":
            current = self.current_page(pages)
            if current.action:
                self.enter_current_page(pages, now)
            else:
                self.leave_section(now)

    def go_home(self, now: float):
        self.mode = "home"
        self.confirm_action = None
        print("ui home", flush=True)
        self.manual_until = 0.0

    def enter_menu(self, now: float):
        self.mode = "cover"
        self.group_index = 0
        self.confirm_action = None
        print("ui menu", flush=True)
        self.wake_manual(now, MENU_IDLE_SECONDS)

    def toggle_home_lock(self):
        if self.home_frame_lock is None:
            self.home_frame_lock = self.home_visible_frame
            print(f"ui home lock frame={self.home_frame_lock}", flush=True)
        else:
            print(f"ui home unlock frame={self.home_frame_lock}", flush=True)
            self.home_frame_lock = None

    def next_home_frame(self):
        self.home_visible_frame = (self.home_visible_frame + 1) % 3
        if self.home_frame_lock is not None:
            self.home_frame_lock = self.home_visible_frame
            print(f"ui home next locked frame={self.home_visible_frame}", flush=True)
        else:
            print(f"ui home next frame={self.home_visible_frame}", flush=True)

    def arm_confirm(self, item: str, now: float):
        self.mode = "confirm"
        self.confirm_action = item
        if item in {"Reboot", "Shutdown"}:
            self.set_message(self.action_label(item), f"Hold OK {self.confirm_seconds():.0f}s", now, 1.2)
        else:
            self.set_message(self.action_label(item), "OK", now, 1.2)

    def execute_confirm(self, now: float):
        action = self.confirm_action
        self.mode = "section"
        self.confirm_action = None
        if action == "UpdateRefreshRun":
            ok = start_systemd_job(
                "proxnap-lcd-update",
                [COPY_UPDATE_CONTROL, "--config", COPY_UPDATE_CONFIG, "--run-upgrade"],
            )
            self.set_message("5.1.1 Update", "Started" if ok else "Start failed", now, 2.5)
        elif action == "UpdateRefresh":
            ok = start_systemd_job(
                "proxnap-lcd-check-updates",
                [COPY_UPDATE_CONTROL, "--config", COPY_UPDATE_CONFIG, "--check-updates"],
            )
            self.set_message("5.1.2 Refresh", "Started" if ok else "Start failed", now, 2.5)
        elif action == "UpdateRun":
            ok = start_systemd_job(
                "proxnap-lcd-run-updates",
                [COPY_UPDATE_CONTROL, "--config", COPY_UPDATE_CONFIG, "--run-install"],
            )
            self.set_message("5.1.3 Run", "Started" if ok else "Start failed", now, 2.5)
        elif action == "Reboot":
            ok = start_systemd_job("proxnap-lcd-reboot", [COPY_UPDATE_CONTROL, "--config", COPY_UPDATE_CONFIG, "--reboot"])
            self.set_message("5.2 Reboot", "Starting" if ok else "Start failed", now, 2.5)
        elif action == "Shutdown":
            ok = start_systemd_job("proxnap-lcd-poweroff", [COPY_UPDATE_CONTROL, "--config", COPY_UPDATE_CONFIG, "--poweroff"])
            self.set_message("5.3 Shutdown", "Starting" if ok else "Start failed", now, 2.5)

    def enter_cover(self, now: float):
        self.mode = "section"
        group = self.current_group()
        self.page_by_group[group] = 0
        print(f"ui enter group={group}", flush=True)
        self.wake_manual(now, MENU_IDLE_SECONDS)

    def leave_section(self, now: float):
        self.mode = "cover"
        group = self.current_group()
        print(f"ui leave group={group}", flush=True)
        self.wake_manual(now, MENU_IDLE_SECONDS)

    def enter_current_page(self, pages: list[LcdPage], now: float):
        current = self.current_page(pages)
        if current.action == "fan_mode":
            self.mode = "fan"
            print("ui fan menu", flush=True)
            self.set_message("Fan mode", current_fan_mode(), now)
        elif current.action == "updates_menu":
            self.mode = "updates"
            self.update_index = 0
            print("ui updates menu", flush=True)
            self.set_message(*self.update_lines(), now)
        elif current.action in self.DANGEROUS:
            self.arm_confirm(current.action, now)
        else:
            self.leave_section(now)

    def short_press(self, mask: int, now: float, pages_count: int, pages: list[LcdPage]):
        self.wake_manual(now)
        if self.mode == "home":
            if mask == self.SELECT:
                self.next_home_frame()
            elif mask == self.ENTER:
                self.enter_menu(now)
            return

        if self.mode == "cover":
            if mask == self.SELECT:
                self.group_step(1)
                print(f"ui next group={self.current_group()}", flush=True)
            elif mask == self.ENTER:
                self.enter_cover(now)
            return

        if self.mode == "section":
            if mask == self.SELECT:
                self.next_page_or_group(pages_count)
            elif mask == self.ENTER:
                self.enter_or_return(now, pages)
            return

        if self.mode == "fan":
            if mask == self.SELECT:
                mode = next_fan_mode(1)
                ok = set_fan_mode(mode)
                print(f"ui fan mode={mode} ok={ok}", flush=True)
                self.set_message("Fan mode", mode if ok else "set failed", now)
            elif mask == self.ENTER:
                self.mode = "section"
                self.set_message("Fan mode", "Saved", now)
            return

        if self.mode == "updates":
            if mask == self.SELECT:
                self.update_index = (self.update_index + 1) % len(self.UPDATE_ITEMS)
                print(f"ui updates item={self.update_index + 1}", flush=True)
                self.set_message(*self.update_lines(), now)
            elif mask == self.ENTER:
                _label, _detail, action = self.UPDATE_ITEMS[self.update_index % len(self.UPDATE_ITEMS)]
                self.confirm_action = action
                self.execute_confirm(now)
            return

        if self.mode == "confirm":
            if mask == self.SELECT:
                self.back(now)
            elif mask == self.ENTER:
                if self.confirm_action in {"Reboot", "Shutdown"}:
                    self.set_message(self.action_label(self.confirm_action), f"Hold OK {self.confirm_seconds():.0f}s", now)
                    print("ui ok short ignored power confirm", flush=True)
                else:
                    print(f"ui confirm action={self.confirm_action}", flush=True)
                    self.execute_confirm(now)

    def long_press(self, mask: int, now: float, pages_count: int, pages: list[LcdPage]):
        self.wake_manual(now, MENU_IDLE_SECONDS)
        if self.mode == "home":
            if mask == self.SELECT:
                self.next_home_frame()
            elif mask == self.ENTER:
                self.toggle_home_lock()
            return

        if self.mode == "cover":
            if mask == self.ENTER:
                self.enter_cover(now)
            elif mask == self.SELECT:
                self.group_step(1)
                print(f"ui next group={self.current_group()}", flush=True)
            return

        if self.mode == "section":
            if mask == self.ENTER:
                self.enter_or_return(now, pages)
            elif mask == self.SELECT:
                self.next_page_or_group(pages_count)
            return

        if self.mode == "fan":
            if mask == self.SELECT:
                mode = next_fan_mode(1)
                ok = set_fan_mode(mode)
                print(f"ui fan mode={mode} ok={ok}", flush=True)
                self.set_message("Fan mode", mode if ok else "set failed", now)
            elif mask == self.ENTER:
                self.mode = "section"
                self.set_message("Fan mode", "Saved", now)
            return

        if self.mode == "updates":
            if mask == self.SELECT:
                self.update_index = (self.update_index + 1) % len(self.UPDATE_ITEMS)
                print(f"ui updates item={self.update_index + 1}", flush=True)
                self.set_message(*self.update_lines(), now)
            elif mask == self.ENTER:
                _label, _detail, action = self.UPDATE_ITEMS[self.update_index % len(self.UPDATE_ITEMS)]
                self.confirm_action = action
                self.execute_confirm(now)
            return

        if self.mode == "confirm":
            if mask == self.ENTER:
                if self.confirm_action in {"Reboot", "Shutdown"}:
                    print(f"ui confirm action={self.confirm_action}", flush=True)
                    self.execute_confirm(now)
                else:
                    self.execute_confirm(now)
            elif mask == self.SELECT:
                self.back(now)

    def process_mask(self, mask: int, now: float, pages_count: int, pages: list[LcdPage]):
        if mask and not self.press_mask:
            self.press_mask = mask
            self.press_started = now
            self.long_handled = False
            return

        if mask == 0 and self.press_mask:
            held = now - self.press_started
            press = self.press_mask
            long_done = self.long_handled
            self.press_mask = 0
            self.long_handled = False
            if (
                not long_done
                and BUTTON_MIN_PRESS_SECONDS <= held
                and not (press == self.last_short_mask and now - self.last_short_at < BUTTON_DEBOUNCE_SECONDS)
            ):
                self.last_short_mask = press
                self.last_short_at = now
                self.short_press(press, now, pages_count, pages)

    def tick(self, now: float, pages_count: int, pages: list[LcdPage]) -> bool:
        if self.mode == "home" and self.press_mask == self.ENTER:
            threshold = HOME_LOCK_SECONDS
        elif self.mode == "confirm" and self.press_mask == self.ENTER and self.confirm_action in {"Reboot", "Shutdown"}:
            threshold = POWER_CONFIRM_SECONDS
        else:
            return False
        if self.press_mask and not self.long_handled and now - self.press_started >= threshold:
            self.long_press(self.press_mask, now, pages_count, pages)
            self.long_handled = True
            return True
        return False

    def advance_page(self, pages_count: int):
        if self.mode == "cover":
            self.group_step(1)
        elif self.mode == "section":
            self.next_page_or_group(pages_count)

    def lines(self, pages: list[LcdPage], now: float) -> tuple[str, str]:
        if self.override and now < self.override_until:
            return self.override
        self.override = None
        if self.mode == "fan":
            return self.fan_lines()
        if self.mode == "updates":
            return self.update_lines()
        if self.mode == "confirm":
            return self.confirm_lines()
        if not pages:
            return "PROXNAP LCD", "no pages"
        if self.mode == "home":
            return "0. Status", "Loading"
        if self.mode == "cover":
            group = self.current_group()
            line1 = f"{(self.group_index % len(GROUPS)) + 1}. {group}"
            return line1, self.cover_summary(pages)
        group_pages = self.current_group_pages(pages)
        index = self.page_index(pages)
        current = group_pages[index % len(group_pages)]
        return add_section_prefix(current.line1, self.group_index % len(GROUPS), index), current.line2

    def should_rotate(self, now: float) -> bool:
        return self.mode in {"cover", "section"} and now >= self.manual_until


def temperature_text(value: float | None) -> str:
    """Missing or malformed readings are not zero-temperature measurements."""
    if value is None or not math.isfinite(value) or not 0 < value < 130:
        return "?"
    return f"{value:.0f}C"


def build_pages(settings: Settings, prev_net: NetSnapshot, cur_net: NetSnapshot, prev_cpu: tuple[int, int] | None, cur_cpu: tuple[int, int]) -> list[LcdPage]:
    pages: list[LcdPage] = []
    used, total, swap_used, swap_total = read_mem()
    cpu_pct = cpu_usage(prev_cpu, cur_cpu)
    ghz = cpu_freq_ghz()

    ram_line = f"RAM {fmt_mem_pair(used * 1024, total * 1024)}"
    pages.append(page("Status", "Proxmox", pve_short(), f"{uptime_short().replace('up', 'Up', 1)} {load_compact()}"))
    pages.append(page("Status", "CPU/RAM", f"CPU {cpu_pct:.0f}% {ghz:.1f}G", ram_line))
    pages.extend(page("Network", f"IP {iface}", "IP", ip) for iface, ip in ip_pages(settings))

    visible = visible_interfaces(settings)
    primary = primary_interfaces(settings, visible)
    if primary:
        rx_rate, tx_rate, rx_total, tx_total = net_line(primary, prev_net, cur_net)
        label = "+".join(primary)[:10]
        pages.append(page("Network", f"Net {label}", f"Net {label}", f"R{fmt_rate_lcd(rx_rate)} T{fmt_rate_lcd(tx_rate)}"))
        pages.append(page("Network", f"Total {label}", f"Total {label}", f"R{fmt_bytes(rx_total)} T{fmt_bytes(tx_total)}"))

    for iface in [iface for iface in visible if iface not in set(primary)]:
        rx_rate, tx_rate, rx_total, tx_total = net_line([iface], prev_net, cur_net)
        pages.append(page("Network", f"Iface {iface[:10]}", f"Iface {iface[:10]}", f"R{fmt_rate_lcd(rx_rate)} T{fmt_rate_lcd(tx_rate)}"))
        pages.append(page("Network", f"Total {iface[:9]}", f"Total {iface[:9]}", f"R{fmt_bytes(rx_total)} T{fmt_bytes(tx_total)}"))

    cpu_temp = coretemp_package()
    _pch_temp = max_temp_for_names(("pch_cometlake",))
    load_parts = read_file("/proc/loadavg").split()
    load_detail = f"L{load_parts[0]}/{load_parts[1]}" if len(load_parts) >= 2 else "L?"
    pages.append(page("Thermal", "CPU", f"CPU {cpu_temp:.0f}C {cpu_pct:.0f}%" if cpu_temp else f"CPU {cpu_pct:.0f}%", f"{ghz:.1f}G {load_detail}"))
    nvme_temp = max_temp_for_names(("nvme",))
    hdd_temp = max_temp_for_names(("drivetemp",))
    sys_temp = max_temp_for_names(("acpitz", "pch_cometlake"))
    pages.append(page("Thermal", "Temps", f"Temp CPU {temperature_text(cpu_temp)}", f"N {temperature_text(nvme_temp)} H {temperature_text(hdd_temp)}"))
    pages.append(page("Thermal", "System temp", "Temp system", f"SYS {temperature_text(sys_temp)}"))
    fan_line1, fan_line2 = fan_summary()
    pages.append(page("Thermal", "Fans", fan_line1, fan_line2, "fan_mode"))

    pools = zpool_rows()
    if pools:
        ok = sum(1 for _name, _size, _alloc, health in pools if health == "ONLINE")
        _bad = len(pools) - ok
        size = sum(size for _name, size, _alloc, _health in pools)
        alloc = sum(alloc for _name, _size, alloc, _health in pools)
        pct = (alloc / size * 100) if size else 0
        pages.append(page("Storage", "Pools", f"Pools {ok}/{len(pools)} OK", f"{fmt_bytes(alloc)}/{fmt_bytes(size)} {pct:.0f}%"))
    else:
        pages.append(page("Storage", "Pools", "Pools", "none"))
    svc_line1, svc_line2 = services_page(settings)
    pages.append(page("Status", "Services", svc_line1, svc_line2))
    alerts = alert_pages(settings)
    pages.extend(page("Status", line1, line1, line2) for line1, line2 in alerts)
    pages.append(page("Actions", "Updates", "Updates", "OK", "updates_menu"))
    pages.append(page("Actions", "Reboot", "Reboot", "OK", "Reboot"))
    pages.append(page("Actions", "Shutdown", "Shutdown", "OK", "Shutdown"))
    return pages


def build_live_snapshot(settings: Settings, prev_net: NetSnapshot, cur_net: NetSnapshot) -> LiveSnapshot:
    used, total, _swap_used, _swap_total = read_mem()
    load_1, load_5 = parse_loads()
    visible = visible_interfaces(settings)
    primary = primary_interfaces(settings, visible)
    if primary:
        rx_rate, tx_rate, _rx_total, _tx_total = net_line(primary, prev_net, cur_net)
    else:
        rx_rate = tx_rate = 0.0
    vm_count, lxc_count = vm_lxc_counts()
    total_gb = total / (1024 ** 2) if total else 0.0
    used_gb = used / (1024 ** 2) if used else 0.0
    ram_percent = (100.0 * used / total) if total else 0.0
    return LiveSnapshot(
        hostname=socket.gethostname(),
        load_1=load_1,
        load_5=load_5,
        ram_used_gb=used_gb,
        ram_total_gb=total_gb,
        ram_pct=ram_percent,
        vm_count=vm_count,
        lxc_count=lxc_count,
        uptime_short=uptime_value_short(),
        net_rx_bps=rx_rate,
        net_tx_bps=tx_rate,
    )


class PageCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.pages: list[LcdPage] = [LcdPage("Status", "Loading", "PROXNAP LCD", "Loading")]
        self.snapshot: LiveSnapshot | None = None
        self.updated_at = 0.0
        self.max_age = 30.0

    def set(self, pages: list[LcdPage], snapshot: LiveSnapshot | None = None, max_age: float | None = None):
        if max_age is not None and not (0 < max_age < float("inf")):
            raise ValueError("snapshot max_age must be finite and positive")
        with self.lock:
            self.pages = pages or [LcdPage("Status", "Empty", "PROXNAP LCD", "no pages")]
            # Error/page-only updates must not make old home readings fresh.
            self.snapshot = snapshot
            if max_age is not None:
                self.max_age = float(max_age)
            self.updated_at = time.monotonic()

    def get(self) -> list[LcdPage]:
        with self.lock:
            return list(self.pages)

    def get_snapshot(self) -> LiveSnapshot | None:
        with self.lock:
            age = time.monotonic() - self.updated_at
            if not 0 <= age <= self.max_age:
                return None
            return self.snapshot


def page_worker(config_path: str, cache: PageCache):
    prev_net = all_net_stats()
    cur_net = prev_net
    prev_cpu = cpu_stat()
    cur_cpu = prev_cpu
    next_refresh = 0.0

    while running:
        mono = time.monotonic()
        if mono < next_refresh:
            time.sleep(min(0.5, next_refresh - mono))
            continue

        settings = load_settings(config_path)
        if not settings.enabled:
            next_refresh = mono + max(2, settings.data_refresh_seconds)
            continue

        try:
            prev_net, cur_net = cur_net, all_net_stats()
            prev_cpu, cur_cpu = cur_cpu, cpu_stat()
            cache.set(
                build_pages(settings, prev_net, cur_net, prev_cpu, cur_cpu),
                build_live_snapshot(settings, prev_net, cur_net),
                max_age=2.0 * max(2, settings.data_refresh_seconds),
            )
        except Exception as exc:
            cache.set([LcdPage("Status", "Error", "LCD data error", str(exc)[:16])])

        next_refresh = time.monotonic() + max(2, settings.data_refresh_seconds)


def run(args) -> int:
    lcd = Lcd(args.lcd_port)
    settings = load_settings(args.config)
    input_buttons = InputButtons(settings.button_input)
    ui = LcdUi()
    page_cache = PageCache()
    worker = threading.Thread(target=page_worker, args=(args.config, page_cache), daemon=True)
    worker_started = False
    last_activity = time.monotonic()
    last_render = 0.0
    last_lines: tuple[str, str] | None = None
    next_config_reload = 0.0
    ignore_until_release = False
    disabled_by_config = not settings.enabled
    needs_init = True
    output_suspended = True
    power_inputs = None
    power_popup = None

    try:
        lcd.open()
        input_buttons.open()
        worker.start()
        worker_started = True
        print(f"LCD daemon on {args.lcd_port}", flush=True)
        while running:
            mono = time.monotonic()
            if mono >= next_config_reload:
                settings = load_settings(args.config)
                next_config_reload = mono + settings.config_reload_seconds

            # Keep draining the existing input sources even during an external
            # message, but do not let hidden UI actions consume these events.
            buttons = []
            if settings.enabled:
                buttons = lcd.read_buttons()
                buttons.extend(input_buttons.read_buttons())
            with lcd_transaction(settings.lcd_message_lock) as owned:
                # Power has its own read-only source, never an Enter/Next mask.
                # Keep draining while another writer owns the panel; discard there.
                power_request = False
                if settings.enabled and settings.power_status_enabled:
                    if power_inputs is None:
                        power_inputs = _PowerStatusInputs()
                    power_inputs.configure(settings.power_status_inputs, mono)
                    power_request = power_inputs.poll(mono, owned and not output_suspended and power_popup is None)
                elif power_inputs is not None:
                    power_inputs.close()
                    power_inputs = None
                if power_popup is not None and (not owned or not settings.enabled or not settings.power_status_enabled):
                    power_popup.finish(ui, mono)
                    power_popup = None
                    last_activity = mono
                    last_lines = None
                    last_render = 0.0
                    ignore_until_release = True
                if not owned:
                    output_suspended = True
                    last_render = 0.0
                    last_lines = None
                    ignore_until_release = bool(buttons[-1]) if buttons else (ignore_until_release or bool(ui.press_mask))
                    ui.reset_press()
                else:
                    if needs_init:
                        lcd.stop_clock()
                        needs_init = False

                    if not settings.enabled:
                        disabled_by_config = True
                        if lcd.backlight_on or output_suspended:
                            lcd.clear()
                            lcd.backlight(False)
                        ui.reset_press()
                        ignore_until_release = False
                        last_lines = None
                        last_render = 0.0
                        output_suspended = False
                    else:
                        if disabled_by_config:
                            disabled_by_config = False
                            output_suspended = True
                            last_activity = mono
                            last_render = 0.0
                            last_lines = None

                        if output_suspended:
                            # Restore a visible screen before accepting a fresh
                            # gesture. Never replay a press/hold across ownership.
                            lcd.backlight(True)
                            ignore_until_release = bool(buttons[-1]) if buttons else ignore_until_release
                            buttons = []
                            ui.reset_press()
                            last_lines = None
                            last_render = 0.0
                            output_suspended = False

                        if power_request and power_popup is None:
                            ignore_until_release = ignore_until_release or bool(ui.press_mask)
                            power_popup = _PowerStatusPopup(ui, mono, settings.power_status_page_seconds)
                            lcd.backlight(True)
                            last_lines = None
                            last_render = 0.0
                        popup_lines = power_popup.lines(mono) if power_popup is not None else None
                        if power_popup is not None and popup_lines is None:
                            power_popup.finish(ui, mono)
                            power_popup = None
                            ui.reset_press()
                            ignore_until_release = bool(buttons[-1]) if buttons else ignore_until_release
                            buttons = []
                            last_activity = mono
                            last_lines = None
                            last_render = 0.0
                        if power_popup is not None and popup_lines is not None:
                            # Drain navigation without hidden tick/process_mask.
                            # A held button must be released before any new gesture.
                            if buttons:
                                ignore_until_release = bool(buttons[-1])
                            ui.reset_press()
                            last_activity = mono
                            lcd.write_lines(*popup_lines)
                            last_lines = popup_lines
                            last_render = mono
                        else:
                            pages = page_cache.get()
                            pages_count = len(ui.current_group_pages(pages))
                            if lcd.backlight_on and last_lines is not None and (buttons or ui.press_mask):
                                # A short external message may fit between polls.
                                # Reassert the visible context before any UI action.
                                lcd.write_lines(*last_lines)
                            for btn in buttons:
                                print(f"button {button_name(btn)} mask=0x{btn:02x}", flush=True)
                                if not lcd.backlight_on:
                                    if btn:
                                        lcd.backlight(True)
                                        ui.go_home(mono)
                                        last_activity = mono
                                        last_render = 0.0
                                        last_lines = None
                                        ignore_until_release = True
                                        ui.reset_press()
                                    continue

                                last_activity = mono
                                if ignore_until_release:
                                    if btn == 0:
                                        ignore_until_release = False
                                    continue

                                ui.process_mask(btn, mono, pages_count, pages)
                                last_render = 0.0
                                last_lines = None

                            if ui.tick(mono, pages_count, pages):
                                last_activity = mono
                                last_render = 0.0
                                last_lines = None

                            if ui.mode != "home" and ui.manual_until and mono >= ui.manual_until:
                                ui.go_home(mono)
                                last_activity = mono
                                ui.home_visible_frame = home_frame_index(mono, settings)
                                last_render = 0.0
                                last_lines = None

                            if lcd.backlight_on and ui.mode == "home" and mono - last_activity > settings.timeout_seconds:
                                lcd.clear()
                                lcd.backlight(False)
                                ignore_until_release = False
                                ui.reset_press()
                                last_lines = None

                            home_render = ui.mode == "home" and mono - last_render >= settings.home_tick_seconds
                            should_rotate = ui.should_rotate(mono) and mono - last_render >= settings.page_seconds
                            should_render = last_render == 0.0 or home_render or should_rotate
                            if lcd.backlight_on and should_render:
                                pages = page_cache.get()
                                if not pages:
                                    pages = [LcdPage("Status", "Empty", "PROXNAP LCD", "no pages")]
                                pages_count = len(ui.current_group_pages(pages))
                                if should_rotate and last_render != 0.0:
                                    ui.advance_page(pages_count)
                                if ui.mode == "home":
                                    line1, line2, frame = home_lines(
                                        page_cache.get_snapshot(),
                                        mono,
                                        settings,
                                        ui.home_frame_lock,
                                        ui.home_visible_frame,
                                    )
                                    ui.home_visible_frame = frame
                                else:
                                    line1, line2 = ui.lines(pages, mono)
                                # flock is not a change counter: another writer may
                                # run entirely between polls. Refresh even equal lines.
                                lcd.write_lines(line1, line2)
                                last_lines = (line1, line2)
                                last_render = mono

            time.sleep(0.2 if settings.enabled else 1.0)
    finally:
        try:
            if args.leave_lcd_off:
                with lcd_transaction(settings.lcd_message_lock) as owned:
                    if owned:
                        lcd.clear()
                        lcd.backlight(False)
        finally:
            try:
                if power_inputs is not None:
                    power_inputs.close()
            finally:
                try:
                    input_buttons.close()
                finally:
                    try:
                        lcd.close()
                    finally:
                        if worker_started:
                            stop()
                            worker.join(timeout=1.0)
    return 0


def lcd_off(args) -> int:
    settings = load_settings(args.config)
    with lcd_transaction(settings.lcd_message_lock) as owned:
        if not owned:
            return 1
        lcd = Lcd(args.lcd_port)
        try:
            lcd.open()
            lcd.clear()
            lcd.backlight(False)
        finally:
            lcd.close()
    return 0


def once(args) -> int:
    settings = load_settings(args.config)
    prev_net = all_net_stats()
    prev_cpu = cpu_stat()
    time.sleep(1)
    cur_net = all_net_stats()
    cur_cpu = cpu_stat()
    for item in build_pages(settings, prev_net, cur_net, prev_cpu, cur_cpu):
        print(f"{item.group:<7} | {fit(item.line1)} | {fit(item.line2)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lcd-port", default=LCD_PORT)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run")
    p_run.add_argument("--leave-lcd-off", action="store_true")
    p_run.set_defaults(func=run)

    p_once = sub.add_parser("once")
    p_once.set_defaults(func=once)

    p_off = sub.add_parser("off")
    p_off.set_defaults(func=lcd_off)

    args = parser.parse_args()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
