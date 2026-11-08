#!/usr/bin/env python3
"""
Collect Proxnap metrics and write them directly to InfluxDB 2.x.

The collector intentionally avoids Telegraf so QNAP-specific metrics can use
stable, useful labels such as fan group, physical-ish device identity, zpool and
Proxnap status. It never hard-codes /sys/class/hwmon/hwmonN indexes.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


HWMON_ROOT = Path("/sys/class/hwmon")
NET_ROOT = Path("/sys/class/net")
CPU_ROOT = Path("/sys/devices/system/cpu")
STATUS_JSON = Path("/run/proxnap-status.json")
DEFAULT_SERVICES = (
    "proxnap-lcd", "proxnap-fan-control", "proxnap-status-led",
    "proxnap-panel-breath", "proxnap-copy-button", "proxnap-power-button",
    "qnap8528-load-module", "pveproxy", "pvedaemon", "pvestatd", "zfs-zed", "smartd",
)


@dataclass
class Settings:
    host: str = field(default_factory=socket.gethostname)
    influx_url: str = ""
    influx_org: str = "proxnap"
    influx_bucket: str = "proxnap"
    influx_token: str = ""
    precision: str = "s"
    interval_fast: int = 10
    interval_medium: int = 30
    interval_qnap: int = 120
    interval_slow: int = 300
    timeout: int = 8
    once: bool = False
    dry_run: bool = False
    measurement_prefix: str = "proxnap"
    services: tuple[str, ...] = DEFAULT_SERVICES

    def __post_init__(self) -> None:
        validate_measurement_prefix(self.measurement_prefix)
        validate_services(self.services)


def validate_measurement_prefix(prefix: str) -> str:
    """Allow a literal ASCII namespace, never line-protocol syntax."""
    if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", prefix):
        raise ValueError("PROXNAP_METRICS_MEASUREMENT_PREFIX must match [A-Za-z][A-Za-z0-9_]*")
    return prefix


def transform_measurement_prefix(encoded: str, prefix: str) -> str:
    """Change only an initial proxnap_ measurement prefix in one encoded point.

    Keep the measurement suffix, escaped tags, fields and timestamp byte-exact.
    Other measurement namespaces pass through; embedded record separators fail.
    """
    validate_measurement_prefix(prefix)
    if not isinstance(encoded, str) or "\n" in encoded or "\r" in encoded:
        raise ValueError("metrics output must contain exactly one line per point")
    if encoded.startswith("proxnap_"):
        return prefix + encoded[len("proxnap"):]
    return encoded


def validate_services(services: tuple[str, ...]) -> tuple[str, ...]:
    """Validate ordered literal unit names, not options, paths or patterns."""
    if not isinstance(services, tuple) or not services:
        raise ValueError("PROXNAP_METRICS_SERVICES must contain at least one unit")
    unit_types = {"service", "socket", "target", "device", "mount", "automount",
                  "swap", "timer", "path", "slice", "scope"}
    for unit in services:
        if (not isinstance(unit, str) or
                not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:@-]*", unit) or
                unit.count("@") > 1 or unit.endswith("@")):
            raise ValueError("PROXNAP_METRICS_SERVICES contains an invalid literal unit name")
        if "." in unit:
            stem, suffix = unit.rsplit(".", 1)
            if suffix not in unit_types or not stem:
                raise ValueError("PROXNAP_METRICS_SERVICES contains an invalid unit suffix")
            full_name = unit
        else:
            full_name = unit + ".service"
        if len(full_name) > 255:
            raise ValueError("PROXNAP_METRICS_SERVICES contains an overlong unit name")
    if len(set(services)) != len(services):
        raise ValueError("PROXNAP_METRICS_SERVICES contains duplicate units")
    return services


def parse_services(raw: str) -> tuple[str, ...]:
    """Parse CSV with optional ASCII spaces around each nonempty unit."""
    if not isinstance(raw, str) or any(char in raw for char in "\r\n\t"):
        raise ValueError("PROXNAP_METRICS_SERVICES must be a single-line CSV of units")
    return validate_services(tuple(unit.strip(" ") for unit in raw.split(",")))


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def load_settings() -> Settings:
    return Settings(
        host=os.environ.get("PROXNAP_METRICS_HOST", socket.gethostname()),
        influx_url=os.environ.get("PROXNAP_INFLUX_URL", "").rstrip("/"),
        influx_org=os.environ.get("PROXNAP_INFLUX_ORG", "proxnap"),
        influx_bucket=os.environ.get("PROXNAP_INFLUX_BUCKET", "proxnap"),
        influx_token=os.environ.get("PROXNAP_INFLUX_TOKEN", ""),
        precision=os.environ.get("PROXNAP_INFLUX_PRECISION", "s"),
        interval_fast=env_int("PROXNAP_METRICS_INTERVAL_FAST", 10),
        interval_medium=env_int("PROXNAP_METRICS_INTERVAL_MEDIUM", 30),
        interval_qnap=env_int("PROXNAP_METRICS_INTERVAL_QNAP", 120),
        interval_slow=env_int("PROXNAP_METRICS_INTERVAL_SLOW", 300),
        timeout=env_int("PROXNAP_METRICS_TIMEOUT", 8),
        measurement_prefix=os.environ.get("PROXNAP_METRICS_MEASUREMENT_PREFIX", "proxnap"),
        services=parse_services(os.environ.get("PROXNAP_METRICS_SERVICES", ",".join(DEFAULT_SERVICES))),
    )


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return default


def read_int(path: Path) -> int | None:
    try:
        return int(read_text(path))
    except ValueError:
        return None


def run_json(cmd: list[str], timeout: int) -> Any | None:
    try:
        out = subprocess.check_output(cmd, text=True, timeout=timeout)
        return json.loads(out)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


def run_lines(cmd: list[str], timeout: int) -> list[str]:
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=timeout)
        return [line for line in out.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        return []


def block_size_sectors(dev: str) -> int | None:
    return read_int(Path("/sys/class/block") / dev / "size")


def run_smart_lines(cmd: list[str], timeout: int) -> list[str]:
    """smartctl health bits are data; command/open/parse failures are not."""
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=timeout)
    except subprocess.CalledProcessError as exc:
        if exc.returncode < 0 or exc.returncode & 0b111:
            return []
        out = exc.output or ""
    except (OSError, subprocess.SubprocessError):
        return []
    return [row for row in out.splitlines() if row.strip()]


def esc_tag(value: Any) -> str:
    text = str(value)
    return text.replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def esc_measurement(value: str) -> str:
    return value.replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,")


def esc_string(value: Any) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def field_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value}i"
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return f"{value:.6g}"
    return esc_string(value)


def line(measurement: str, tags: dict[str, Any], fields: dict[str, Any], ts: int) -> str | None:
    good_fields = []
    for key, value in fields.items():
        encoded = field_value(value)
        if encoded is not None:
            good_fields.append(f"{esc_tag(key)}={encoded}")
    if not good_fields:
        return None
    tag_text = "".join(f",{esc_tag(k)}={esc_tag(v)}" for k, v in sorted(tags.items()) if v not in (None, ""))
    return f"{esc_measurement(measurement)}{tag_text} {','.join(good_fields)} {ts}"


def emit(lines: list[str], measurement: str, tags: dict[str, Any], fields: dict[str, Any], ts: int) -> None:
    encoded = line(measurement, tags, fields, ts)
    if encoded:
        lines.append(encoded)


def hwmons_by_name() -> dict[str, list[Path]]:
    found: dict[str, list[Path]] = {}
    for path in sorted(HWMON_ROOT.glob("hwmon*")):
        name = read_text(path / "name")
        if name:
            found.setdefault(name, []).append(path)
    return found


def block_identity(timeout: int) -> dict[str, dict[str, str]]:
    data = run_json(["lsblk", "-J", "-dn", "-o", "NAME,MODEL,SERIAL,SIZE,TYPE,ROTA"], timeout)
    result: dict[str, dict[str, str]] = {}
    for item in (data or {}).get("blockdevices", []):
        name = item.get("name")
        if not name:
            continue
        result[name] = {
            "model": (item.get("model") or "").strip(),
            "serial": (item.get("serial") or "").strip(),
            "size": item.get("size") or "",
            "type": item.get("type") or "",
            "rota": str(item.get("rota", "")),
            "size_sectors": str(block_size_sectors(name) or 0),
        }
    return result


def disk_slot_map() -> dict[str, str]:
    """Optional local serial-to-slot mapping; no reference-machine identities."""
    raw = os.environ.get("PROXNAP_METRICS_SLOT_MAP", "{}")
    value = json.loads(raw)
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ValueError("PROXNAP_METRICS_SLOT_MAP must be a JSON object of strings")
    return value


def collect_hwmon(
    lines: list[str],
    settings: Settings,
    ts: int,
    identities: dict[str, dict[str, str]],
    *,
    include_qnap_ec: bool = True,
) -> None:
    slots = disk_slot_map()
    for name, paths in hwmons_by_name().items():
        for idx, path in enumerate(paths):
            if name == "qnap8528":
                if not include_qnap_ec:
                    continue
                for f in sorted(path.glob("temp*_input")):
                    raw = read_int(f)
                    if raw is not None:
                        emit(lines, "proxnap_temperature", {"host": settings.host, "source": "qnap8528", "sensor": f.stem.replace("_input", "")}, {"temperature_c": raw / 1000.0}, ts)
                for f in sorted(path.glob("fan*_input")):
                    raw = read_int(f)
                    if raw is not None:
                        fan = f.stem.replace("_input", "")
                        group = "cpu" if fan in {"fan7", "fan8"} else "rear"
                        emit(lines, "proxnap_fan", {"host": settings.host, "group": group, "fan": fan}, {"rpm": raw}, ts)
                for f in sorted(path.glob("pwm*")):
                    if not re.fullmatch(r"pwm\d+", f.name):
                        continue
                    raw = read_int(f)
                    if raw is not None:
                        group = "cpu" if f.name == "pwm7" else "rear"
                        emit(lines, "proxnap_pwm", {"host": settings.host, "group": group, "pwm": f.name}, {"raw": raw, "percent": raw * 100.0 / 255.0}, ts)
                continue

            if name == "coretemp":
                for f in sorted(path.glob("temp*_input")):
                    raw = read_int(f)
                    label = read_text(path / f.name.replace("_input", "_label"), f.stem.replace("_input", ""))
                    if raw is not None:
                        emit(lines, "proxnap_temperature", {"host": settings.host, "source": "coretemp", "sensor": label}, {"temperature_c": raw / 1000.0}, ts)
                continue

            if name == "drivetemp":
                block = ""
                blocks = list((path / "device" / "block").glob("*"))
                if blocks:
                    block = blocks[0].name
                ident = identities.get(block, {})
                raw = read_int(path / "temp1_input")
                if raw is not None:
                    emit(
                        lines,
                        "proxnap_temperature",
                        {
                            "host": settings.host,
                            "source": "drivetemp",
                            "dev": block,
                            "model": ident.get("model", ""),
                            "serial": ident.get("serial", ""),
                            "slot": slots.get(ident.get("serial", ""), ""),
                        },
                        {"temperature_c": raw / 1000.0},
                        ts,
                    )
                continue

            if name == "nvme":
                ctrl = Path(os.path.realpath(path / "device")).name
                # Best effort: controller nvmeX usually maps to namespace nvmeXn1.
                ns = f"{ctrl}n1" if ctrl.startswith("nvme") else ctrl
                ident = identities.get(ns, {})
                for f in sorted(path.glob("temp*_input")):
                    raw = read_int(f)
                    label = read_text(path / f.name.replace("_input", "_label"), f.stem.replace("_input", ""))
                    if raw is not None:
                        emit(
                            lines,
                            "proxnap_temperature",
                            {
                                "host": settings.host,
                                "source": "nvme",
                                "dev": ns,
                                "controller": ctrl,
                                "sensor": label,
                                "model": ident.get("model", ""),
                                "serial": ident.get("serial", ""),
                                "slot": slots.get(ident.get("serial", ""), ""),
                            },
                            {"temperature_c": raw / 1000.0},
                            ts,
                        )
                continue

            for f in sorted(path.glob("temp*_input")):
                raw = read_int(f)
                label = read_text(path / f.name.replace("_input", "_label"), f.stem.replace("_input", ""))
                if raw is not None:
                    emit(lines, "proxnap_temperature", {"host": settings.host, "source": name, "sensor": label, "index": idx}, {"temperature_c": raw / 1000.0}, ts)


def parse_proc_stat() -> dict[str, tuple[int, int]]:
    stats = {}
    for line_text in read_text(Path("/proc/stat")).splitlines():
        parts = line_text.split()
        if not parts or not parts[0].startswith("cpu"):
            continue
        values = [int(x) for x in parts[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        stats[parts[0]] = (total, idle)
    return stats


def collect_cpu(lines: list[str], settings: Settings, ts: int, prev: dict[str, Any]) -> None:
    load = read_text(Path("/proc/loadavg")).split()
    if len(load) >= 3:
        emit(lines, "proxnap_cpu", {"host": settings.host, "scope": "load"}, {"load1": float(load[0]), "load5": float(load[1]), "load15": float(load[2])}, ts)
    current = parse_proc_stat()
    old = prev.get("cpu_stat", {})
    for cpu, (total, idle) in current.items():
        if cpu in old:
            old_total, old_idle = old[cpu]
            dt = total - old_total
            di = idle - old_idle
            if dt > 0:
                usage = 100.0 * (dt - di) / dt
                emit(lines, "proxnap_cpu", {"host": settings.host, "cpu": cpu}, {"usage_percent": usage}, ts)
    prev["cpu_stat"] = current
    freqs = []
    for f in sorted(CPU_ROOT.glob("cpu[0-9]*/cpufreq/scaling_cur_freq")):
        raw = read_int(f)
        if raw is None:
            continue
        cpu = f.parts[-3]
        mhz = raw / 1000.0
        freqs.append(mhz)
        emit(lines, "proxnap_cpu", {"host": settings.host, "cpu": cpu}, {"frequency_mhz": mhz}, ts)
    if freqs:
        emit(lines, "proxnap_cpu", {"host": settings.host, "scope": "frequency"}, {"avg_mhz": sum(freqs) / len(freqs), "max_mhz": max(freqs), "min_mhz": min(freqs)}, ts)


def collect_mem(lines: list[str], settings: Settings, ts: int) -> None:
    values = {}
    for row in read_text(Path("/proc/meminfo")).splitlines():
        key, rest = row.split(":", 1)
        parts = rest.strip().split()
        if parts and parts[0].isdigit():
            values[key] = int(parts[0]) * 1024
    total = values.get("MemTotal", 0)
    avail = values.get("MemAvailable", 0)
    swap_total = values.get("SwapTotal", 0)
    swap_free = values.get("SwapFree", 0)
    fields = {
        "mem_total_bytes": total,
        "mem_available_bytes": avail,
        "mem_used_bytes": total - avail if total else 0,
        "mem_used_percent": 100.0 * (total - avail) / total if total else 0,
        "swap_total_bytes": swap_total,
        "swap_used_bytes": swap_total - swap_free if swap_total else 0,
        "swap_used_percent": 100.0 * (swap_total - swap_free) / swap_total if swap_total else 0,
    }
    emit(lines, "proxnap_memory", {"host": settings.host}, fields, ts)


def collect_net(lines: list[str], settings: Settings, ts: int, prev: dict[str, Any]) -> None:
    now = time.monotonic()
    old = prev.get("net", {})
    current = {}
    for iface in sorted(NET_ROOT.iterdir()):
        if iface.name == "lo" or not (iface / "statistics").exists():
            continue
        rx = read_int(iface / "statistics" / "rx_bytes") or 0
        tx = read_int(iface / "statistics" / "tx_bytes") or 0
        current[iface.name] = (now, rx, tx)
        fields = {"rx_bytes": rx, "tx_bytes": tx, "up": 1 if read_text(iface / "operstate") in {"up", "unknown"} else 0}
        if iface.name in old:
            old_now, old_rx, old_tx = old[iface.name]
            dt = max(0.001, now - old_now)
            fields["rx_bps"] = (rx - old_rx) * 8.0 / dt
            fields["tx_bps"] = (tx - old_tx) * 8.0 / dt
        emit(lines, "proxnap_network", {"host": settings.host, "iface": iface.name}, fields, ts)
    prev["net"] = current


def collect_zfs(lines: list[str], settings: Settings, ts: int) -> None:
    for row in run_lines(["zpool", "list", "-Hp", "-o", "name,size,alloc,free,cap,health,frag"], settings.timeout):
        parts = row.split("\t")
        if len(parts) >= 7:
            name, size, alloc, free, cap, health, frag = parts[:7]
            health_code = {"ONLINE": 0, "DEGRADED": 1, "FAULTED": 2, "UNAVAIL": 3}.get(health, 9)
            emit(lines, "proxnap_zpool", {"host": settings.host, "pool": name, "health": health}, {"size_bytes": int(size), "alloc_bytes": int(alloc), "free_bytes": int(free), "capacity_percent": int(cap), "fragmentation_percent": int(frag), "health_code": health_code}, ts)
    for row in run_lines(["zfs", "list", "-Hp", "-o", "name,used,avail,refer,mountpoint", "-t", "filesystem,volume"], settings.timeout):
        parts = row.split("\t")
        if len(parts) >= 5:
            name, used, avail, refer, mount = parts[:5]
            pool = name.split("/", 1)[0]
            emit(lines, "proxnap_zfs_dataset", {"host": settings.host, "pool": pool, "dataset": name, "mountpoint": mount}, {"used_bytes": int(used), "avail_bytes": int(avail), "refer_bytes": int(refer)}, ts)
    arc = {}
    for row in read_text(Path("/proc/spl/kstat/zfs/arcstats")).splitlines()[2:]:
        parts = row.split()
        if len(parts) >= 3 and parts[2].isdigit():
            arc[parts[0]] = int(parts[2])
    if arc:
        hits = arc.get("hits", 0)
        misses = arc.get("misses", 0)
        fields = {k: arc.get(k, 0) for k in ("size", "c", "c_min", "c_max", "hits", "misses", "demand_data_hits", "demand_data_misses", "demand_metadata_hits", "demand_metadata_misses")}
        fields["hit_ratio"] = hits / (hits + misses) if hits + misses else 0
        emit(lines, "proxnap_zfs_arc", {"host": settings.host}, fields, ts)


def collect_pve(lines: list[str], settings: Settings, ts: int) -> None:
    for row in run_lines(["pvesm", "status"], settings.timeout)[1:]:
        parts = row.split()
        if len(parts) >= 7:
            name, typ, status, total, used, avail, pct = parts[:7]
            emit(lines, "proxnap_pve_storage", {"host": settings.host, "storage": name, "type": typ, "status": status}, {"total_bytes": int(total) * 1024, "used_bytes": int(used) * 1024, "available_bytes": int(avail) * 1024, "used_percent": float(pct.rstrip("%"))}, ts)
    guests = run_json(["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"], settings.timeout)
    for g in guests or []:
        gid = str(g.get("vmid", ""))
        typ = g.get("type", "")
        status = g.get("status", "unknown")
        emit(lines, "proxnap_pve_guest", {"host": settings.host, "guest_id": gid, "guest_type": typ, "name": g.get("name", ""), "status": status}, {"running": 1 if status == "running" else 0, "cpu": float(g.get("cpu") or 0), "mem_bytes": int(g.get("mem") or 0), "maxmem_bytes": int(g.get("maxmem") or 0), "disk_bytes": int(g.get("disk") or 0), "maxdisk_bytes": int(g.get("maxdisk") or 0), "uptime_seconds": int(g.get("uptime") or 0)}, ts)


def collect_diskio(lines: list[str], settings: Settings, ts: int, prev: dict[str, Any], identities: dict[str, dict[str, str]]) -> None:
    now = time.monotonic()
    old = prev.get("diskio", {})
    current = {}
    wanted = set(identities)
    for row in read_text(Path("/proc/diskstats")).splitlines():
        parts = row.split()
        if len(parts) < 14:
            continue
        dev = parts[2]
        if dev not in wanted:
            continue
        reads = int(parts[3])
        sectors_r = int(parts[5])
        writes = int(parts[7])
        sectors_w = int(parts[9])
        io_ms = int(parts[12])
        current[dev] = (now, reads, sectors_r, writes, sectors_w, io_ms)
        ident = identities.get(dev, {})
        fields = {"reads": reads, "writes": writes, "read_bytes": sectors_r * 512, "write_bytes": sectors_w * 512, "io_ms": io_ms}
        if dev in old:
            old_now, old_reads, old_sr, old_writes, old_sw, old_io = old[dev]
            dt = max(0.001, now - old_now)
            fields.update({
                "read_bps": (sectors_r - old_sr) * 512.0 / dt,
                "write_bps": (sectors_w - old_sw) * 512.0 / dt,
                "read_iops": (reads - old_reads) / dt,
                "write_iops": (writes - old_writes) / dt,
                "io_ms_per_s": (io_ms - old_io) / dt,
            })
        emit(lines, "proxnap_disk_io", {"host": settings.host, "dev": dev, "model": ident.get("model", ""), "serial": ident.get("serial", ""), "slot": disk_slot_map().get(ident.get("serial", ""), "")}, fields, ts)
    prev["diskio"] = current


def collect_services(lines: list[str], settings: Settings, ts: int) -> None:
    for svc in validate_services(settings.services):
        active = subprocess.call(["systemctl", "is-active", "--quiet", svc], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        enabled = subprocess.call(["systemctl", "is-enabled", "--quiet", svc], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        emit(lines, "proxnap_service", {"host": settings.host, "service": svc}, {"active": 1 if active == 0 else 0, "enabled": 1 if enabled == 0 else 0}, ts)
    collect_status(lines, settings, ts)


def collect_status(lines: list[str], settings: Settings, ts: int) -> None:
    # Legacy producers use the existing default 30s check period. New producers
    # publish their actual period, so stale data cannot be re-timestamped healthy.
    state = "unknown"
    fields: dict[str, Any] = {"status_code": 9, "status_available": 0}
    try:
        status = json.loads(read_text(STATUS_JSON))
        if not isinstance(status, dict):
            raise ValueError("status must be an object")
        stamp = status.get("timestamp")
        period = status.get("check_seconds", 30)
        if type(stamp) is not int or stamp <= 0:
            raise ValueError("invalid timestamp")
        if type(period) not in {int, float} or not math.isfinite(period) or period <= 0:
            raise ValueError("invalid check period")
        fields["source_timestamp"] = stamp
        fields["source_age_seconds"] = ts - stamp
        reasons, warnings = status.get("reasons"), status.get("warnings")
        unavailable = status.get("unavailable", [])
        if not isinstance(reasons, list) or not isinstance(warnings, list):
            raise ValueError("invalid reason lists")
        if not all(isinstance(items, list) and all(isinstance(x, str) for x in items)
                   for items in (reasons, warnings, unavailable)):
            raise ValueError("invalid reason lists")
        source_state = status.get("status")
        if not isinstance(source_state, str):
            raise ValueError("invalid state")
        coherent = ((source_state == "ok" and not reasons and not warnings) or
                    (source_state == "warning" and warnings and not reasons) or
                    (source_state == "critical" and reasons))
        if not (coherent and (not unavailable or source_state == "critical") and
                0 <= ts - stamp <= period):
            raise ValueError("unknown, incomplete or stale status")
        state = source_state
        fields.update(status_code={"ok": 0, "warning": 1, "critical": 2}[state],
                      status_available=int(not unavailable), warning_count=len(warnings),
                      critical_count=len(reasons))
    except (ValueError, TypeError, OverflowError):
        pass
    emit(lines, "proxnap_proxnap_status", {"host": settings.host, "status": state}, fields, ts)


def collect_smart(lines: list[str], settings: Settings, ts: int, identities: dict[str, dict[str, str]]) -> None:
    slots = disk_slot_map()
    for dev, ident in identities.items():
        if ident.get("type") != "disk" or dev.startswith("zd") or dev == "sdm":
            continue
        sectors = block_size_sectors(dev)
        if not sectors:
            disk_type = "nvme" if dev.startswith("nvme") else "ata"
            emit(
                lines,
                "proxnap_disk_smart",
                {
                    "host": settings.host,
                    "dev": dev,
                    "type": disk_type,
                    "model": ident.get("model", ""),
                    "serial": ident.get("serial", ""),
                    "slot": slots.get(ident.get("serial", ""), ""),
                },
                {"present": 0, "size_sectors": sectors or 0, "smart_skipped": 1, "smart_available": 0},
                ts,
            )
            continue
        path = f"/dev/{dev}"
        if dev.startswith("nvme"):
            rows = run_lines(["nvme", "smart-log", path], settings.timeout)
            fields: dict[str, Any] = {}
            for r in rows:
                if ":" not in r:
                    continue
                k, v = [x.strip() for x in r.split(":", 1)]
                num = re.search(r"-?\d+", v.replace(",", ""))
                if not num:
                    continue
                key = k.lower().replace(" ", "_")
                if key in {"temperature", "available_spare", "percentage_used", "media_errors", "num_err_log_entries", "unsafe_shutdowns", "power_cycles", "power_on_hours"}:
                    fields[key] = int(num.group(0))
            if fields:
                fields["present"] = 1
                fields["size_sectors"] = sectors
                emit(lines, "proxnap_disk_smart", {"host": settings.host, "dev": dev, "type": "nvme", "model": ident.get("model", ""), "serial": ident.get("serial", ""), "slot": slots.get(ident.get("serial", ""), "")}, fields, ts)
            continue
        rows = run_smart_lines(["smartctl", "-A", "-H", "-n", "standby", path], settings.timeout)
        text = "\n".join(rows)
        if "STANDBY" in text.upper():
            emit(lines, "proxnap_disk_smart", {"host": settings.host, "dev": dev, "type": "ata", "model": ident.get("model", ""), "serial": ident.get("serial", ""), "slot": slots.get(ident.get("serial", ""), "")}, {"standby": 1, "smart_available": 0}, ts)
            continue
        fields = {"present": 1, "size_sectors": sectors, "smart_available": 0}
        health = []
        malformed_health = False
        for r in rows:
            if r.startswith(("SMART overall-health self-assessment test result:", "SMART Health Status:")):
                token = r.split(":", 1)[1].strip().upper()
                if token in {"PASSED", "OK", "FAILED"}:
                    health.append(token != "FAILED")
                else:
                    malformed_health = True
        if health and not malformed_health and len(set(health)) == 1:
            fields.update(standby=0, smart_available=1, smart_passed=int(health[0]))
        elif False in health:
            fields["smart_passed"] = 0
        attr_map = {"Reallocated_Sector_Ct": "reallocated_sector_ct", "Current_Pending_Sector": "current_pending_sector", "Offline_Uncorrectable": "offline_uncorrectable", "UDMA_CRC_Error_Count": "udma_crc_error_count", "Power_On_Hours": "power_on_hours", "Power_Cycle_Count": "power_cycle_count", "Temperature_Celsius": "temperature_c", "Airflow_Temperature_Cel": "airflow_temperature_c"}
        for r in rows:
            parts = r.split()
            if len(parts) >= 10 and parts[1] in attr_map:
                try:
                    fields[attr_map[parts[1]]] = int(parts[-1])
                except ValueError:
                    pass
        emit(lines, "proxnap_disk_smart", {"host": settings.host, "dev": dev, "type": "ata", "model": ident.get("model", ""), "serial": ident.get("serial", ""), "slot": slots.get(ident.get("serial", ""), "")}, fields, ts)


def write_influx(settings: Settings, lines: list[str]) -> None:
    lines = [transform_measurement_prefix(row, settings.measurement_prefix) for row in lines]
    if settings.dry_run:
        print("\n".join(lines))
        return
    if not lines:
        return
    if not settings.influx_url or not settings.influx_token:
        raise RuntimeError("PROXNAP_INFLUX_URL and PROXNAP_INFLUX_TOKEN are required")
    query = urllib.parse.urlencode({"org": settings.influx_org, "bucket": settings.influx_bucket, "precision": settings.precision})
    req = urllib.request.Request(
        f"{settings.influx_url}/api/v2/write?{query}",
        data=("\n".join(lines) + "\n").encode(),
        headers={"Authorization": f"Token {settings.influx_token}", "Content-Type": "text/plain; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=settings.timeout) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"Influx write failed: {e.code} {detail}") from e


def collect_all(settings: Settings, prev: dict[str, Any], tier: str, *, include_qnap_ec: bool = False) -> list[str]:
    ts = int(time.time())
    lines: list[str] = []
    identities = block_identity(settings.timeout)
    collect_hwmon(lines, settings, ts, identities, include_qnap_ec=include_qnap_ec)
    collect_cpu(lines, settings, ts, prev)
    collect_mem(lines, settings, ts)
    collect_net(lines, settings, ts, prev)
    collect_diskio(lines, settings, ts, prev, identities)
    collect_services(lines, settings, ts)
    if tier in {"medium", "slow"}:
        collect_zfs(lines, settings, ts)
        collect_pve(lines, settings, ts)
    if tier == "slow":
        collect_smart(lines, settings, ts, identities)
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    settings = load_settings()
    settings.once = args.once
    settings.dry_run = args.dry_run
    prev: dict[str, Any] = {}
    next_medium = 0.0
    next_qnap = 0.0
    next_slow = 0.0
    while True:
        now = time.monotonic()
        tier = "fast"
        include_qnap_ec = settings.once or now >= next_qnap
        if include_qnap_ec:
            next_qnap = now + settings.interval_qnap
        if now >= next_slow:
            tier = "slow"
            next_slow = now + settings.interval_slow
            next_medium = now + settings.interval_medium
        elif now >= next_medium:
            tier = "medium"
            next_medium = now + settings.interval_medium
        lines = collect_all(settings, prev, tier, include_qnap_ec=include_qnap_ec)
        write_influx(settings, lines)
        qnap_tag = " qnap_ec=1" if include_qnap_ec else ""
        print(f"wrote {len(lines)} lines tier={tier}{qnap_tag}", flush=True)
        if settings.once:
            return 0
        time.sleep(max(1, settings.interval_fast))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
