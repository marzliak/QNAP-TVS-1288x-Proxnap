"""OFFLINE contracts: AST-selected definitions, fixture IO and inert effects only.

Run with explicit TMPDIR and PYTHONDONTWRITEBYTECODE=1. No daemon import,
production sysfs/file IO, service command, LED driver or network client is used.
Adversarial regressions execute current package definitions only.
"""
from __future__ import annotations

import ast
import __future__
import builtins
import contextlib
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from urllib.parse import urlencode

HERE = Path(__file__).resolve().parent
PASS = "SMART overall-health self-assessment test result: PASSED"
FAIL = "SMART overall-health self-assessment test result: FAILED"
METRIC_DEFS = {
    "field_value", "esc_tag", "esc_measurement", "esc_string", "line", "emit",
    "collect_hwmon", "hwmons_by_name", "read_int", "collect_status",
    "collect_services", "collect_smart", "run_smart_lines", "write_influx",
    "collect_all", "validate_measurement_prefix", "transform_measurement_prefix",
    "validate_services",
}
STATUS_DEFS = {
    "max_temp_c", "smart_bad", "failed_units", "health_reasons", "update_reasons",
    "status_snapshot", "unknown_status", "write_status_state",
}


def refuse(*args, **kwargs):
    raise AssertionError("unexpected dependency/effect: " + repr((args, kwargs)))


def source(filename, baseline=False):
    names = {"proxnap-metrics.py": "metrics.py", "proxnap-status-led.py": "status_led.py"}
    if baseline:
        raise AssertionError("Historical source is not a public runtime dependency")
    return (HERE.parent / "src/proxnap" / names[filename]).read_text(encoding="utf-8")


def selected(filename, names, namespace, baseline=False):
    tree = ast.parse(source(filename, baseline), filename=filename)
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
             and node.name in names]
    # No module imports, module assignments, class defaults, main or actuators.
    assert all(node.name not in {"main", "set_led", "set_status", "set_copy",
                                  "sh", "run_command", "maybe_buzz"} for node in nodes)
    module = ast.Module(body=[*nodes], type_ignores=[])
    safe_builtins = dict(vars(builtins), open=refuse, __import__=refuse)
    namespace.update(__builtins__=safe_builtins)
    exec(compile(module, filename, "exec", flags=__future__.annotations.compiler_flag), namespace)
    return namespace


class Fixture:
    def __init__(self, root, baseline=False):
        self.root = root.resolve()
        self.hw = self.root / "hwmon"
        self.hw.mkdir()
        self.clock = 1000
        self.flags = set()
        self.reads = []
        self.command_result: tuple[int, str] = (0, "Listing... Done")
        self.systemd_result: tuple[int, str] = (0, "")
        self.smart_result: tuple[int, str] = (0, PASS)
        self.disk_rows = [PASS]
        self.sectors = 1024
        self.disks = []
        self.effects = []
        self.network = Mock(side_effect=refuse)
        self.command = Mock(side_effect=self.command_call)
        self.status_file = self.root / "status.json"
        self.status_writes = []
        self.settings = SimpleNamespace(host="fixture", timeout=1, check_seconds=30,
            critical_temp_c=85, reallocated_red_threshold=100, update_check_seconds=21600,
            dry_run=True, influx_url="https://inert.invalid", influx_token="fixture-only",
            influx_org="fixture", influx_bucket="fixture", precision="s",
            measurement_prefix="proxnap", services=("fixture-one", "fixture-two"))
        fake_subprocess = SimpleNamespace(DEVNULL=-3, call=Mock(return_value=0),
            check_output=Mock(side_effect=refuse), CalledProcessError=subprocess.CalledProcessError,
            SubprocessError=subprocess.SubprocessError)
        fake_os = SimpleNamespace(path=SimpleNamespace(join=os.path.join,
            realpath=self.realpath, exists=self.exists, isdir=self.isdir),
            replace=Mock(side_effect=self.replace))
        self.m = selected("proxnap-metrics.py", METRIC_DEFS, {
            "Path": Path, "Any": object, "Settings": object, "json": json,
            "math": math, "re": re, "os": fake_os, "subprocess": fake_subprocess,
            "HWMON_ROOT": self.hw, "STATUS_JSON": self.status_file,
            "time": SimpleNamespace(time=lambda: self.clock),
            "read_text": self.read_text, "disk_slot_map": lambda: {},
            "block_size_sectors": lambda dev: self.sectors,
            "run_lines": Mock(side_effect=lambda *a, **k: list(self.disk_rows)),
            "urllib": SimpleNamespace(parse=SimpleNamespace(urlencode=urlencode),
                request=SimpleNamespace(Request=lambda *a, **k: SimpleNamespace(args=a, **k),
                                        urlopen=self.network),
                error=SimpleNamespace(HTTPError=type("InertHTTPError", (Exception,), {}))),
        }, baseline)
        # Explicit mocked reader for ATA; separately test the actual adapter.
        self.m["run_smart_lines"] = Mock(side_effect=lambda *a, **k: list(self.disk_rows))
        self.s = selected("proxnap-status-led.py", STATUS_DEFS, {
            "Settings": object, "json": json, "re": re, "os": fake_os,
            "glob": SimpleNamespace(glob=self.glob), "read_file": self.read_text,
            "time": SimpleNamespace(monotonic=lambda: self.clock, time=lambda: self.clock),
            "known_temp_paths": set(), "last_update_check": 0.0, "last_update_reasons": [],
            "UPDATE_RUNNING_FLAG": "/run/proxnap-update-running",
            "STATUS_STATE_FILE": str(self.status_file), "sh": self.command,
            "zfs_bad": lambda: None,
            "root_read_only": lambda: None, "configured_disks": lambda *args: list(self.disks),
            "write_file": Mock(side_effect=self.write_status),
        }, baseline)

    def safe(self, path):
        path = Path(path).resolve()
        if not path.is_relative_to(self.root):
            raise AssertionError("production path refused: " + str(path))
        return path

    def read_text(self, path, default=""):
        path = self.safe(path)
        self.reads.append(str(path))
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return default

    def realpath(self, path):
        return str(self.safe(path))

    def exists(self, path):
        if str(path) in {"/run/proxnap-update-running", "/var/run/reboot-required"}:
            return str(path) in self.flags
        return self.safe(path).exists()

    def isdir(self, path):
        if str(path) == "/sys/module/qnap8528":
            return True  # inert identity fixture; NOT a live module read
        return self.safe(path).is_dir()

    def glob(self, pattern):
        if pattern == "/sys/class/hwmon/hwmon*":
            return [str(path) for path in sorted(self.hw.glob("hwmon*"))]
        path = self.safe(pattern)
        return [str(p) for p in sorted(path.parent.glob(path.name))]

    def command_call(self, args, **kwargs):
        if args == ["systemctl", "--failed", "--no-legend", "--plain"]:
            return self.systemd_result
        if args == ["apt", "list", "--upgradable"]:
            return self.command_result
        if len(args) == 5 and args[:3] == ["/usr/sbin/smartctl", "-H", "-A"]:
            raise AssertionError("invalid SMART fixture command")
        if args[:3] == ["/usr/sbin/smartctl", "-H", "-A"] and len(args) == 4:
            return self.smart_result
        return refuse(args, kwargs)

    def write_status(self, path, text):
        self.safe(path)
        self.status_writes.append(json.loads(text))
        return True

    def replace(self, old, new):
        self.safe(old)
        self.safe(new)
        self.effects.append(("replace", str(old), str(new)))

    def device(self, name, files, directory=None):
        directory = directory or f"hwmon{len(list(self.hw.iterdir()))}"
        path = self.hw / directory
        path.mkdir()
        (path / "name").write_text(name, encoding="ascii")
        for filename, value in files.items():
            (path / filename).write_text(str(value), encoding="ascii")
        return path

    def snapshot(self):
        return self.s["status_snapshot"](self.settings)

    def status_payload(self, **updates):
        payload = {"status": "ok", "reasons": [], "warnings": [], "timestamp": self.clock}
        payload.update(updates)
        self.status_file.write_text(json.dumps(payload), encoding="utf-8")

    def status_lines(self):
        lines = []
        function = self.m.get("collect_status", self.m["collect_services"])
        function(lines, self.settings, self.clock)
        return [row for row in lines if row.startswith("proxnap_proxnap_status,")]

    def smart_lines(self):
        lines = []
        self.m["collect_smart"](lines, self.settings, self.clock,
                               {"sda": {"type": "disk"}})
        return lines




class OfflineCase(unittest.TestCase):
    def setUp(self):
        if os.environ.get("PYTHONDONTWRITEBYTECODE") != "1":
            self.fail("set PYTHONDONTWRITEBYTECODE=1 explicitly")
        scratch = Path(os.environ["TMPDIR"]).resolve()
        if str(scratch) in {"/tmp", "/var/tmp", "/root"}:
            self.fail("dedicated explicit scratch required")
        self.temp = tempfile.TemporaryDirectory(prefix="proxnap-sensor-contract-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.f = Fixture(Path(self.temp.name))
        self.addCleanup(lambda: self.f.network.assert_not_called())


class BaselineTests(OfflineCase):
    def test_adversarial_sources_cannot_reproduce_false_health(self):
        # Exercise the former false-green categories against current bytes.
        errors = []
        self.assertIsNone(self.f.s["max_temp_c"](errors))
        self.assertTrue(errors)
        disk = self.f.device("coretemp", {"temp1_input": "50000", "temp2_input": "60000"})
        self.assertEqual(self.f.s["max_temp_c"]([]), 60)
        (disk / "temp2_input").unlink()
        missing = []
        self.assertEqual(self.f.s["max_temp_c"](missing), 50)
        self.assertTrue(missing)
        self.assertEqual(self.f.snapshot()[0], "unknown")
        (disk / "temp2_input").write_text("--1")
        missing = []
        self.assertEqual(self.f.s["max_temp_c"](missing), 50)
        self.assertTrue(missing)
        self.assertEqual(self.f.snapshot()[0], "unknown")
        self.f.smart_result = (2, "device open failed")
        unavailable = []
        self.assertIsNone(self.f.s["smart_bad"]("sda", self.f.settings, unavailable))
        self.assertTrue(unavailable)
        self.f.status_payload(timestamp=1)
        self.assertNotIn("status_code=0i", self.f.status_lines()[0])
        self.f.status_file.unlink()
        self.assertNotIn("status_code=0i", self.f.status_lines()[0])
        self.f.disk_rows = []
        self.assertNotIn("smart_passed=1i", self.f.smart_lines()[0])
        self.f.disk_rows = ["NOT PASSED: unsupported health"]
        self.assertNotIn("smart_passed=1i", self.f.smart_lines()[0])
        self.f.s["update_reasons"](self.f.settings, [])
        self.f.flags.add("/var/run/reboot-required")
        self.assertIn("reboot required", self.f.s["update_reasons"](self.f.settings, []))
        self.assertEqual(self.f.network.call_count, 0)


class TemperatureTests(OfflineCase):
    def test_missing_entire_inventory_is_unknown_not_zero(self):
        errors = []
        self.assertIsNone(self.f.s["max_temp_c"](errors))
        self.assertTrue(errors)
        self.assertEqual(self.f.snapshot()[0], "unknown")

    def test_healthy_partial_disappearance_recovery(self):
        path = self.f.device("coretemp", {"temp1_input": "50000", "temp2_input": "60000"})
        self.assertEqual(self.f.snapshot()[0], "ok")
        (path / "temp2_input").unlink()
        self.assertEqual(self.f.snapshot()[0], "unknown")
        (path / "temp2_input").write_text("60000")
        self.assertEqual(self.f.snapshot()[0], "ok")

    def test_removed_whole_device_while_other_device_survives(self):
        a = self.f.device("coretemp", {"temp1_input": "60000"})
        self.f.device("jc42", {"temp1_input": "35000"})
        self.assertEqual(self.f.snapshot()[0], "ok")
        (a / "temp1_input").unlink()
        (a / "name").unlink()
        a.rmdir()
        self.assertEqual(self.f.snapshot()[0], "unknown")

    def test_invalid_malformed_nonfinite_then_recovery(self):
        path = self.f.device("coretemp", {"temp1_input": "50000", "temp2_input": "60000"})
        for value in ("", "bad", "NaN", "Infinity", "-Infinity", "0", "130000", "-1", "--1", "²"):
            with self.subTest(value=value):
                (path / "temp2_input").write_text(value)
                self.assertEqual(self.f.snapshot()[0], "unknown")
                (path / "temp2_input").write_text("60000")
                self.assertEqual(self.f.snapshot()[0], "ok")

    def test_known_hot_alarm_survives_partial_unknown(self):
        self.f.device("coretemp", {"temp1_input": "90000", "temp2_input": "bad"})
        state, reasons, _, unavailable = self.f.snapshot()
        self.assertEqual(state, "critical")
        self.assertTrue(reasons)
        self.assertTrue(unavailable)

    def test_nvme_is_not_read_or_replaced_by_a_command(self):
        self.f.device("coretemp", {"temp1_input": "50000"})
        nvme = self.f.device("nvme", {"temp1_input": "90000"})
        self.assertEqual(self.f.snapshot()[0], "ok")
        self.assertNotIn(str(nvme / "temp1_input"), self.f.reads)
        self.assertEqual(self.f.command.call_args.args[0], ["apt", "list", "--upgradable"])

    def test_unknown_effects_require_explicit_inert_callbacks(self):
        with self.assertRaises(TypeError):
            self.f.s["unknown_status"]()
        effects = []
        self.f.s["unknown_status"](status_writer=lambda x: effects.append(("status", x)),
                                    copy_writer=lambda x: effects.append(("copy", x)))
        self.assertEqual(effects, [("status", 0), ("copy", 0)])


class SmartTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.f.device("coretemp", {"temp1_input": "50000"})
        self.f.disks = ["sda"]
        self.f.smart_result = (0, PASS)

    def test_status_command_failure_and_recovery(self):
        self.assertEqual(self.f.snapshot()[0], "ok")
        for result in ((2, "open failed"), (127, "missing executable"), (-9, ""),
                       (0, ""), (0, "NOT PASSED"), (0, PASS.replace("PASSED", "FUTURE"))):
            with self.subTest(result=result):
                self.f.smart_result = result
                self.assertEqual(self.f.snapshot()[0], "unknown")
                self.f.smart_result = (0, PASS)
                self.assertEqual(self.f.snapshot()[0], "ok")

    def test_true_smart_failure_and_attributes_remain_critical(self):
        for result in ((8, FAIL), (0, PASS + "\n197 Current_Pending_Sector x x x x x x x 1"),
                       (0, PASS + "\n5 Reallocated_Sector_Ct x x x x x x x 100")):
            with self.subTest(result=result):
                self.f.smart_result = result
                self.assertEqual(self.f.snapshot()[0], "critical")

    def test_malformed_known_attribute_is_unavailable(self):
        for row in ("197 Current_Pending_Sector x x x x x x x NaN",
                    "197 Current_Pending_Sector x x x x x x x -1",
                    "197 Current_Pending_Sector"):
            self.f.smart_result = (0, PASS + "\n" + row)
            self.assertEqual(self.f.snapshot()[0], "unknown")

    def test_missing_config_or_unresolved_mapping_is_unavailable_not_empty_healthy(self):
        self.f.s.update(DISK_CONFIG=str(self.f.root / "disk-map"),
                        resolve_block_dev=lambda value: "sda" if self.f.sectors else None)
        selected("proxnap-status-led.py", {"configured_disks"}, self.f.s)
        self.assertEqual(self.f.snapshot()[0], "unknown")
        (self.f.root / "disk-map").write_text("panel_brightness=25\nhdd1=/fixture-disk\n")
        self.assertEqual(self.f.snapshot()[0], "ok")
        self.f.sectors = 0
        self.assertEqual(self.f.snapshot()[0], "unknown")
        self.f.sectors = 1024
        self.assertEqual(self.f.snapshot()[0], "ok")

    def test_metric_empty_or_misleading_rows_do_not_assert_awake_or_healthy(self):
        for rows in ([], ["NOT PASSED"], [PASS.replace("PASSED", "NOT PASSED")],
                     [PASS, PASS.replace("PASSED", "FUTURE")]):
            with self.subTest(rows=rows):
                self.f.disk_rows = rows
                point = self.f.smart_lines()[0]
                self.assertIn("smart_available=0i", point)
                self.assertNotIn("smart_passed=1i", point)
                self.assertNotIn("standby=0i", point)
        self.f.disk_rows = [PASS]
        self.assertIn("smart_passed=1i", self.f.smart_lines()[0])
        self.f.disk_rows = [FAIL]
        self.assertIn("smart_passed=0i", self.f.smart_lines()[0])

    def test_missing_device_does_not_run_commands(self):
        self.f.sectors = 0
        self.assertIn("present=0i", self.f.smart_lines()[0])
        self.f.m["run_smart_lines"].assert_not_called()
        self.f.m["run_lines"].assert_not_called()

    def test_health_exit_bits_output_is_parsed_not_dropped(self):
        ns = selected("proxnap-metrics.py", {"run_smart_lines"}, dict(self.f.m))
        mock_process = ns["subprocess"].check_output
        for rc, expected in ((8, [FAIL]), (16, [FAIL]), (2, []), (127, []), (-9, [])):
            with self.subTest(rc=rc):
                mock_process.side_effect = subprocess.CalledProcessError(rc, ["inert"], output=FAIL)
                self.assertEqual(ns["run_smart_lines"](["inert"], 1), expected)


class CacheTests(OfflineCase):
    def test_runtime_flags_are_not_cached_with_packages(self):
        self.f.device("coretemp", {"temp1_input": "50000"})
        self.assertEqual(self.f.snapshot()[0], "ok")
        self.f.flags.add("/var/run/reboot-required")
        self.assertEqual(self.f.snapshot()[0], "warning")
        self.f.flags.clear()
        self.assertEqual(self.f.snapshot()[0], "ok")
        self.f.flags.add("/run/proxnap-update-running")
        self.assertEqual(self.f.snapshot()[0], "warning")
        self.f.flags.clear()
        self.assertEqual(self.f.snapshot()[0], "ok")
        apt_calls = [call for call in self.f.command.call_args_list
                     if call.args[0] == ["apt", "list", "--upgradable"]]
        self.assertEqual(len(apt_calls), 1)

    def test_failed_update_check_does_not_cache_healthy(self):
        self.f.device("coretemp", {"temp1_input": "50000"})
        self.f.command_result = (100, "failed")
        self.assertEqual(self.f.snapshot()[0], "unknown")
        self.f.command_result = (0, "Listing... Done")
        self.assertEqual(self.f.snapshot()[0], "ok")

    def test_expired_package_cache_failure_invalidates_previous_green(self):
        self.f.device("coretemp", {"temp1_input": "50000"})
        self.assertEqual(self.f.snapshot()[0], "ok")
        self.f.clock += 21600
        self.f.command_result = (100, "failed")
        self.assertEqual(self.f.snapshot()[0], "unknown")

    def test_status_payload_has_explicit_period_and_unavailability(self):
        self.f.s["write_status_state"]("unknown", [], [], ["sensor absent"], 45)
        self.assertEqual(self.f.status_writes, [{"status": "unknown", "reasons": [],
            "warnings": [], "timestamp": 1000, "unavailable": ["sensor absent"],
            "check_seconds": 45}])
        self.assertEqual(len(self.f.effects), 1)


class FailedUnitSourceTests(OfflineCase):
    def consumer_roundtrip(self):
        state, reasons, warnings, unavailable = self.f.snapshot()
        self.f.s["write_status_state"](state, reasons, warnings, unavailable, 30)
        self.f.status_file.write_text(json.dumps(self.f.status_writes[-1]))
        tree = ast.parse((HERE.parent / "src/proxnap/lcd.py").read_text())
        nodes: list[ast.stmt] = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in {"short_status_reason", "critical_status_page"}]
        self.assertEqual(len(nodes), 2)
        def status_open(path, mode="r", encoding="utf-8"):
            self.assertEqual((path, mode, encoding), (str(self.f.status_file), "r", "utf-8"))
            return io.StringIO(self.f.status_file.read_text())
        namespace = {"json": json, "re": re, "LCD_WIDTH": 16,
                     "STATUS_STATE_FILE": str(self.f.status_file), "open": status_open,
                     "time": SimpleNamespace(time=lambda: self.f.clock)}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "<lcd-status-only>", "exec",
                     flags=__future__.annotations.compiler_flag), namespace)
        return state, reasons, unavailable, namespace["critical_status_page"](), self.f.status_lines()[0]

    def test_failed_query_is_unavailable_not_no_failed_units(self):
        for rc in (-9, -1, 1, 127):
            for output in ("", "Failed to connect to bus", "partial.service loaded failed failed"):
                with self.subTest(rc=rc, output=output):
                    self.f.systemd_result = (rc, output)
                    unavailable = []
                    self.assertIsNone(self.f.s["failed_units"](unavailable))
                    self.assertEqual(len(unavailable), 1)
                    self.assertIn("systemd", unavailable[0])

    def test_query_failure_producer_lcd_metric_and_recovery(self):
        self.f.device("coretemp", {"temp1_input": "50000"})
        effects = []
        for result, expected in (((0, ""), "ok"), ((-9, ""), "unknown"), ((0, ""), "ok")):
            self.f.systemd_result = result
            self.f.clock += 1
            state, reasons, unavailable, page, point = self.consumer_roundtrip()
            self.assertEqual(state, expected)
            if expected == "unknown":
                self.assertTrue(unavailable)
                self.assertNotEqual(page[1], "All running OK")
                self.assertIn("status_available=0i", point)
                self.assertIn("status_code=9i", point)
                self.f.s["unknown_status"](status_writer=lambda x: effects.append(("status", x)),
                                            copy_writer=lambda x: effects.append(("copy", x)))
            else:
                self.assertFalse(reasons or unavailable)
                self.assertEqual(page, ("System Check", "All running OK"))
                self.assertIn("status_available=1i", point)
                self.assertIn("status_code=0i", point)
        self.assertEqual(effects, [("status", 0), ("copy", 0)])

    def test_known_critical_not_hidden_by_service_query_failure(self):
        self.f.device("coretemp", {"temp1_input": "95000"})
        self.f.systemd_result = (-9, "")
        state, reasons, unavailable, page, point = self.consumer_roundtrip()
        self.assertEqual(state, "critical")
        self.assertTrue(reasons and unavailable)
        self.assertEqual(page[0], "Critical")
        self.assertIn("status_code=2i", point)
        self.assertIn("status_available=0i", point)

    def test_successful_query_preserves_actual_failed_units(self):
        self.f.device("coretemp", {"temp1_input": "50000"})
        self.f.systemd_result = (0, "fixture.service loaded failed failed Fixture\n")
        state, reasons, _, page, point = self.consumer_roundtrip()
        self.assertEqual(state, "critical")
        self.assertTrue(any("fixture.service" in x for x in reasons))
        self.assertEqual(page[0], "Critical")
        self.assertIn("status_code=2i", point)


class StatusMetricTests(OfflineCase):
    def assertUnknown(self):
        point = self.f.status_lines()[0]
        self.assertIn("status=unknown", point)
        self.assertIn("status_available=0i", point)
        self.assertIn("status_code=9i", point)
        self.assertNotIn("warning_count=0i", point)
        self.assertNotIn("critical_count=0i", point)
        self.assertNotIn("source_timestamp=0i", point)
        return point

    def test_healthy_missing_stale_future_then_recovery(self):
        self.f.status_payload()
        self.assertIn("status_code=0i", self.f.status_lines()[0])
        self.f.status_file.unlink()
        self.assertUnknown()
        self.f.status_payload(timestamp=969)
        self.assertUnknown()
        self.f.status_payload(timestamp=1001)
        self.assertUnknown()
        self.f.status_payload()
        self.assertIn("status_code=0i", self.f.status_lines()[0])

    def test_schema_malformed_partial_nonfinite_then_recovery(self):
        for text in ("{", "null", "[]", "0", '{"status":"ok"}',
                     '{"status":"ok","timestamp":NaN}'):
            with self.subTest(text=text):
                self.f.status_file.write_text(text)
                self.assertUnknown()
        for changes in ({"timestamp": True}, {"timestamp": 0}, {"warnings": ""},
                        {"reasons": None}, {"check_seconds": float("inf")},
                        {"check_seconds": float("nan")}, {"check_seconds": True},
                        {"status": "future"}, {"unavailable": ["sensor absent"]},
                        {"status": "ok", "reasons": ["bad"]}):
            with self.subTest(changes=changes):
                self.f.status_payload(**changes)
                self.assertUnknown()
        self.f.status_payload()
        self.assertIn("status_code=0i", self.f.status_lines()[0])

    def test_new_period_expiry_and_legacy_boundary(self):
        self.f.status_payload(timestamp=970)
        self.assertIn("status_code=0i", self.f.status_lines()[0])
        self.f.status_payload(timestamp=969, check_seconds=45)
        self.assertIn("status_code=0i", self.f.status_lines()[0])
        self.f.clock = 1015
        self.assertUnknown()

    def test_known_critical_not_hidden_by_partial_sensor_failure(self):
        self.f.status_payload(status="critical", reasons=["overtemperature"],
                              unavailable=["one sensor lost"])
        point = self.f.status_lines()[0]
        self.assertIn("status_code=2i", point)
        self.assertIn("status_available=0i", point)

    def test_invalid_status_dry_run_never_publishes_success_or_calls_network(self):
        self.f.status_payload(timestamp=1)
        lines = self.f.status_lines()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.f.m["write_influx"](self.f.settings, lines)
        self.assertNotIn("status_code=0i", out.getvalue())
        self.assertIn("status_available=0i", out.getvalue())
        self.f.network.assert_not_called()

    def test_sensor_failure_roundtrip_snapshot_payload_metric_and_inert_effects(self):
        path = self.f.device("coretemp", {"temp1_input": "50000"})
        effects = []
        for raw, expected in (("50000", "ok"), ("NaN", "unknown"), ("50000", "ok")):
            (path / "temp1_input").write_text(raw)
            state, reasons, warnings, unavailable = self.f.snapshot()
            self.assertEqual(state, expected)
            self.f.s["write_status_state"](state, reasons, warnings, unavailable, 30)
            self.f.status_file.write_text(json.dumps(self.f.status_writes[-1]))
            if state == "unknown":
                self.assertUnknown()
                self.f.s["unknown_status"](
                    status_writer=lambda x: effects.append(("status", x)),
                    copy_writer=lambda x: effects.append(("copy", x)))
            else:
                self.assertIn("status_code=0i", self.f.status_lines()[0])
        self.assertEqual(effects, [("status", 0), ("copy", 0)])


class HwmonContractTests(OfflineCase):
    def test_sources_labels_units_and_pwm_metadata(self):
        self.f.device("qnap8528", {"temp6_input": "35000", "fan7_input": "2000",
                                   "pwm7": "127", "pwm7_enable": "1"})
        self.f.device("coretemp", {"temp1_input": "51000", "temp1_label": "Package id 0"})
        self.f.device("drivetemp", {"temp1_input": "41000"})
        self.f.device("nvme", {"temp1_input": "45000", "temp1_label": "Composite"})
        self.f.device("jc42", {"temp1_input": "32000"})
        lines = []
        self.f.m["collect_hwmon"](lines, self.f.settings, 1000, {}, include_qnap_ec=True)
        text = "\n".join(lines)
        for name in ("qnap8528", "coretemp", "drivetemp", "nvme", "jc42"):
            self.assertIn("source=" + name, text)
        self.assertIn("sensor=Package\\ id\\ 0", text)
        self.assertIn("temperature_c=51", text)
        self.assertIn("group=cpu,host=fixture rpm=2000i", text)
        self.assertIn("raw=127i,percent=49.8039", text)
        self.assertNotIn("pwm7_enable", text)

    def test_qnap_slow_gate_performs_no_value_reads(self):
        path = self.f.device("qnap8528", {"temp1_input": "50000", "fan1_input": "2500", "pwm1": "127"})
        lines = []
        self.f.m["collect_hwmon"](lines, self.f.settings, 1000, {}, include_qnap_ec=False)
        self.assertEqual(lines, [])
        self.assertEqual(self.f.reads, [str(path / "name")])

    def test_missing_malformed_sensor_values_are_omitted_not_zero(self):
        self.f.device("coretemp", {"temp1_input": "NaN", "temp2_input": "Infinity"})
        self.f.device("qnap8528", {"fan1_input": "bad", "pwm1": "bad"})
        lines = []
        self.f.m["collect_hwmon"](lines, self.f.settings, 1000, {}, include_qnap_ec=True)
        self.assertEqual(lines, [])

    def test_line_encoder_drops_nonfinite_and_none(self):
        self.assertIsNone(self.f.m["line"]("fixture", {}, {"x": None, "y": math.nan,
            "z": math.inf}, 1000))

    def test_tiers_still_gate_smart_and_qnap(self):
        calls = []
        ns = self.f.m
        ns["block_identity"] = lambda timeout: {}
        for name in ("collect_hwmon", "collect_cpu", "collect_mem", "collect_net",
                     "collect_diskio", "collect_services", "collect_zfs", "collect_pve", "collect_smart"):
            ns[name] = lambda *args, _name=name, **kw: calls.append((_name, kw))
        ns["collect_all"](self.f.settings, {}, "fast", include_qnap_ec=False)
        self.assertNotIn("collect_smart", [name for name, kw in calls])
        self.assertEqual(calls[0], ("collect_hwmon", {"include_qnap_ec": False}))
        calls.clear()
        ns["collect_all"](self.f.settings, {}, "slow", include_qnap_ec=True)
        self.assertIn("collect_smart", [name for name, kw in calls])
        self.assertEqual(calls[0], ("collect_hwmon", {"include_qnap_ec": True}))

    def test_guards_refuse_real_paths_and_unexpected_commands(self):
        for path in ("/sys/class/hwmon", "/dev/sda", "/run/proxnap-status.json"):
            with self.subTest(path=path), self.assertRaises(AssertionError):
                self.f.read_text(path)
        with self.assertRaises(AssertionError):
            self.f.command_call(["systemctl", "restart", "anything"])
        with self.assertRaises(AssertionError):
            self.f.network("https://inert.invalid")
        self.f.network.reset_mock()


if __name__ == "__main__":
    unittest.main()
