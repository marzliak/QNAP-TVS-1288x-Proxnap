"""Offline integration: staged payload, opt-ins, entrypoints and optional code.

No production daemon import, service invocation, UART, real sysfs or network.
"""
from __future__ import annotations

import __future__
import ast
from contextlib import redirect_stdout
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from offline_loader import load_source
from proxnap import common, runtime

ROOT = Path(__file__).resolve().parents[1]


def forbidden(*args, **kwargs):
    raise AssertionError("unmocked final effect")


def definitions(filename, names, namespace):
    tree = ast.parse((ROOT / "src/proxnap" / filename).read_text())
    nodes = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name in names]
    assert {n.name for n in nodes} == set(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<offline-definitions>", "exec",
                 flags=__future__.annotations.compiler_flag), namespace)
    return namespace


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="proxnap-install-", dir=os.environ["TMPDIR"])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stage = self.root / "stage"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.marker = self.root / "FORBIDDEN-EFFECT"
        # This is a negative witness only. The staged path has no final-effect
        # invocation at all; no real binary is used as a mock fallback.
        for command in ("systemctl", "modinfo", "modprobe", "apt-get", "systemd-run", "reboot", "poweroff"):
            path = self.bin / command
            path.write_text(f"#!/bin/sh\nprintf invoked > '{self.marker}'\nexit 99\n")
            path.chmod(0o755)

    def install(self, *args, stage=None):
        result = subprocess.run(["bash", str(ROOT / "scripts/install.sh"),
                                 "--destdir=" + str(stage or self.stage), *args],
                                env={**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                                     "PYTHONDONTWRITEBYTECODE": "1"},
                                capture_output=True, text=True, timeout=20)
        self.assertFalse(self.marker.exists(), result.stdout + result.stderr)
        return result

    def test_complete_default_payload_and_exact_pwm_identity(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        sources = sorted(p.name for p in (ROOT / "src/proxnap").glob("*.py"))
        self.assertEqual(sorted(p.name for p in (self.stage / "opt/proxnap/proxnap").glob("*.py")), sources)
        for name in sources:
            self.assertEqual((ROOT / "src/proxnap" / name).read_bytes(),
                             (self.stage / "opt/proxnap/proxnap" / name).read_bytes())
        for name in ("copy-button.conf", "power-button.conf"):
            config = common.read_config(str(self.stage / "etc/proxnap" / name))
            self.assertEqual(config["enabled"], "false")
            self.assertEqual(config["reboot_enabled"], "false")
            self.assertEqual(config["poweroff_enabled"], "false")
        metrics = self.stage / "etc/proxnap/metrics.env"
        self.assertEqual(metrics.stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.stage / "usr/local/sbin/proxnap-copy-update").is_file())
        self.assertFalse((self.stage / "etc/systemd/system/multi-user.target.wants").exists())
        self.assertEqual(list(self.stage.rglob("*.c")), [])

    def test_opt_in_mutates_real_config_and_keeps_unrelated_comments(self):
        self.assertEqual(self.install().returncode, 0)
        path = self.stage / "etc/proxnap/copy-button.conf"
        with path.open("a") as stream:
            stream.write("\n# operator note\ncustom_key=preserve\n")
        result = self.install("--enable-copy-button", "--allow-reboot")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = path.read_text()
        config = common.read_config(str(path))
        self.assertEqual(config["enabled"], "true")
        self.assertEqual(config["reboot_enabled"], "true")
        self.assertEqual(config["poweroff_enabled"], "false")
        self.assertEqual(config["short_refresh_enabled"], "false")
        self.assertEqual(config["upgrade_enabled"], "false")
        self.assertEqual(config["custom_key"], "preserve")
        self.assertIn("# operator note", text)
        self.assertNotIn("\\n", text)
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(sum(line.startswith("enabled=") for line in text.splitlines()), 1)

    def test_invalid_retained_fan_config_refused_before_any_code_write(self):
        path = self.stage / "etc/proxnap/fan-control.conf"
        path.parent.mkdir(parents=True)
        original = "cpu_min_duty=25\n"
        path.write_text(original)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(path.read_text(), original)
        self.assertFalse((self.stage / "opt").exists())
        self.assertFalse((self.stage / "usr").exists())

    def test_staging_dry_run_has_no_files(self):
        self.assertEqual(self.install("--dry-run").returncode, 0)
        self.assertFalse(self.stage.exists())

    def test_production_root_and_symlink_destinations_are_refused(self):
        self.assertNotEqual(self.install(stage=Path("/")).returncode, 0)
        target = self.root / "foreign"
        target.mkdir()
        self.stage.symlink_to(target)
        self.assertNotEqual(self.install().returncode, 0)
        self.assertEqual(list(target.iterdir()), [])

    def test_nested_symlink_refused_before_any_payload_write(self):
        self.stage.mkdir()
        target = self.root / "foreign"
        target.mkdir()
        (self.stage / "etc").symlink_to(target)
        self.assertNotEqual(self.install().returncode, 0)
        self.assertFalse((self.stage / "opt").exists())
        self.assertEqual(list(target.iterdir()), [])

    def test_staging_uninstall_cannot_reach_production_path(self):
        self.assertNotEqual(self.install("--uninstall").returncode, 0)
        self.assertFalse(self.stage.exists())


class DispatchTests(unittest.TestCase):
    def test_wrong_chassis_never_imports_or_dispatches_any_implementation(self):
        with patch.object(runtime, "require_supported_model", return_value=False), \
                patch.object(runtime.importlib, "import_module", side_effect=forbidden):
            for name in runtime.MODULES:
                with self.subTest(entrypoint=name):
                    self.assertEqual(runtime.dispatch(name, []), 4)

    def test_old_cli_aliases_use_same_guarded_implementation(self):
        captured = []
        module = SimpleNamespace(main=lambda: captured.append(sys.argv[:]) or 0)
        with patch.object(runtime, "require_supported_model", return_value=True), \
                patch.object(runtime.importlib, "import_module", return_value=module) as imports:
            self.assertEqual(runtime.dispatch("fan-control", ["--failsafe"]), 0)
            self.assertEqual(imports.call_args.args, ("proxnap.fan_control",))
            self.assertEqual(captured[-1], ["proxnap-fan-control", "--safe"])
            self.assertEqual(runtime.dispatch("lcd", ["--config", "fixture.conf", "--off"]), 0)
            self.assertEqual(captured[-1], ["proxnap-lcd", "--config", "fixture.conf", "off"])
            self.assertEqual(runtime.dispatch("copy-button", []), 0)
            self.assertEqual(imports.call_args.args, ("proxnap.copy_update",))

    def test_units_use_guarded_dispatch_and_keep_actions_optional(self):
        for path in (ROOT / "systemd").glob("proxnap-*.service"):
            for line in path.read_text().splitlines():
                if line.startswith(("ExecStart=", "ExecStopPost=")):
                    self.assertIn("-m proxnap.runtime ", line)
        installer = (ROOT / "scripts/install.sh").read_text()
        self.assertNotIn("enable --now proxnap-metrics", installer)
        self.assertNotIn("enable --now proxnap-blue-temp", installer)
        self.assertIn("enable_disk=false", installer)
        self.assertIn("enable_power=false", installer)


class ActionOptInTests(unittest.TestCase):
    def test_disabled_copy_effects_never_open_lock_or_spawn(self):
        effects = []
        namespace = {"Settings": SimpleNamespace, "open": forbidden, "subprocess": SimpleNamespace(Popen=forbidden),
                     "log": lambda *args: None, "finish_update_action": lambda *args: effects.append("cleanup")}
        definitions("copy_update.py", {"perform_upgrade", "perform_install_only", "perform_check", "run_system_action"}, namespace)
        settings = SimpleNamespace(upgrade_enabled=False, short_refresh_enabled=False,
                                   reboot_enabled=False, poweroff_enabled=False)
        namespace["lcd_action"] = lambda *args, **kwargs: None
        namespace["perform_upgrade"](settings)
        namespace["perform_install_only"](settings)
        namespace["perform_check"](settings)
        namespace["run_system_action"](settings, "reboot")
        namespace["run_system_action"](settings, "poweroff")
        self.assertEqual(effects, ["cleanup"] * 3)

    def test_manual_copy_cli_reports_blocked_instead_of_success(self):
        settings = SimpleNamespace(short_refresh_enabled=False, upgrade_enabled=False,
                                   reboot_enabled=False, poweroff_enabled=False)
        for requested in ("check_updates", "run_install", "run_upgrade", "reboot", "poweroff"):
            args = SimpleNamespace(config="mock://config", summary=False, check_updates=False,
                                   run_install=False, run_upgrade=False, reboot=False, poweroff=False)
            setattr(args, requested, True)
            parser = SimpleNamespace(add_argument=lambda *a, **k: None, parse_args=lambda: args)
            namespace = {"argparse": SimpleNamespace(ArgumentParser=lambda: parser),
                         "DEFAULT_CONFIG": "mock://config", "load_settings": lambda p: settings,
                         "perform_check": forbidden, "perform_install_only": forbidden,
                         "perform_upgrade": forbidden, "run_system_action": forbidden}
            definitions("copy_update.py", {"main"}, namespace)
            with self.subTest(action=requested):
                self.assertEqual(namespace["main"](), 3)

    def test_disabled_power_effects_never_open_lock_or_spawn(self):
        namespace = {"Settings": SimpleNamespace, "open": forbidden, "subprocess": SimpleNamespace(Popen=forbidden),
                     "log": lambda *args: None}
        definitions("power_button.py", {"run_action"}, namespace)
        settings = SimpleNamespace(reboot_enabled=False, poweroff_enabled=False)
        for action in (None, "reboot", "poweroff", "shutdown"):
            namespace["run_action"](settings, action)


class OptionalPoliciesTests(unittest.TestCase):
    def load(self, name):
        module = load_source(ROOT / "src/proxnap" / name, "offline_optional_" + name.replace(".", "_"))
        self.addCleanup(sys.modules.pop, module.__name__, None)
        return module

    def test_disk_smart_unknown_command_failures_and_critical_are_distinct(self):
        module = self.load("disk_leds.py")
        module.resolve_block_dev = lambda path: "fixture"
        for code, text, expected in ((2, "device open failed", None), (0, "", None),
                                     (0, "NOT PASSED: unsupported", None),
                                     (0, "SMART overall-health self-assessment test result: PASSED", False),
                                     (8, "SMART overall-health self-assessment test result: FAILED", True),
                                     (16, "SMART overall-health self-assessment test result: PASSED", True)):
            with self.subTest(code=code, text=text):
                module.subprocess.run = lambda *args, **kwargs: SimpleNamespace(returncode=code, stdout=text)
                self.assertIs(module.smart_health("mock://disk")[0], expected)

    def test_unknown_smart_never_lights_healthy_activity_and_recovery_does(self):
        module = self.load("disk_leds.py")
        module.Slot.exists = lambda self: True
        slot = module.Slot("hdd1", "mock://disk", last_activity=10)
        self.assertEqual(slot.desired_brightness(10), 0)
        slot.smart_bad = False
        self.assertEqual(slot.desired_brightness(10), 1)
        slot.smart_bad = True
        self.assertEqual(slot.desired_brightness(11), 2)

    def test_bay_map_rejects_path_traversal_duplicates_and_empty_devices(self):
        module = self.load("disk_leds.py")
        for values in (["../../arbitrary=mock"], ["hdd1=mock", "hdd1=other"], ["ssd1="]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                module.parse_map(values, True)
        self.assertEqual(module.parse_map(["hdd8=mock"], True)[0].led, "hdd8")

    def test_panel_off_hours_wrap_midnight_and_static_compatibility(self):
        module = self.load("panel_led.py")
        self.assertTrue(module.in_time_range(23 * 60 + 30, "23:00-08:00"))
        self.assertTrue(module.in_time_range(7 * 60, "23:00-08:00"))
        self.assertFalse(module.in_time_range(12 * 60, "23:00-08:00"))

    def test_lcd_missing_thermal_value_is_not_a_zero_measurement(self):
        namespace = {"math": math}
        definitions("lcd.py", {"temperature_text"}, namespace)
        for value in (None, 0, -1, float("nan"), float("inf")):
            self.assertEqual(namespace["temperature_text"](value), "?")
        self.assertEqual(namespace["temperature_text"](55.5), "56C")

    def test_status_ignores_generic_disk_timing_keys_not_health_sources(self):
        namespace = {"read_file": lambda *args: "poll_seconds=0.25\nactivity_hold_seconds=0.75\nsmart_interval_seconds=300\n",
                     "DISK_CONFIG": "mock://disk-config", "resolve_block_dev": forbidden}
        definitions("status_led.py", {"configured_disks"}, namespace)
        unavailable = []
        self.assertEqual(namespace["configured_disks"](unavailable), [])
        self.assertEqual(unavailable, [])

    def test_blue_temperature_pulse_bounds_and_unknown_are_not_zero(self):
        module = self.load("blue_temp_breath.py")
        module.find_hwmon = lambda name: None
        self.assertIsNone(module.read_cpu_temp_c())
        self.assertGreater(module.cycle_seconds(40), module.cycle_seconds(80))
        for temperature in (40, 60, 70, 80):
            for phase in (0, 0.25, 0.5, 0.75):
                width = module.pulse_width_seconds(temperature, phase)
                self.assertGreater(width, 0)
                self.assertLess(width, module.cycle_seconds(temperature))

    def test_legacy_acpi_policy_is_retained_without_a_service(self):
        module = self.load("acpi_fans.py")
        writes = []
        module.write_file = lambda path, value: writes.append((path, value)) or True
        state = module.FanState(["mock://cooling"])
        with redirect_stdout(io.StringIO()):
            state.apply_policy(75)
            state.apply_policy(60)
            state.apply_policy(50)
        self.assertEqual(writes, [("mock://cooling/cur_state", "1"), ("mock://cooling/cur_state", "0")])
        self.assertFalse(any("acpi_fans" in p.read_text() for p in (ROOT / "systemd").glob("*")))

    def test_optional_fault_reporter_writes_only_fixture_and_clears_it(self):
        with tempfile.TemporaryDirectory(prefix="proxnap-fault-", dir=os.environ["TMPDIR"]) as directory:
            path = Path(directory) / "fault"
            effects = []
            def scratch_open(target, *args, **kwargs):
                self.assertEqual(Path(target), path)
                return open(target, *args, **kwargs)
            namespace = {"FAN_FAULT_FLAG": str(path), "os": SimpleNamespace(
                path=os.path, makedirs=lambda *args, **kwargs: None,
                unlink=lambda target: Path(target).unlink()), "open": scratch_open,
                "set_led": lambda name, value: effects.append((name, value))}
            definitions("fan_diagnostics.py", {"mark_fault", "clear_fault"}, namespace)
            with redirect_stdout(io.StringIO()):
                namespace["mark_fault"]("fixture fault")
            self.assertEqual(path.read_text(), "fixture fault\n")
            self.assertEqual(effects, [("status", 2)])
            namespace["clear_fault"]()
            namespace["clear_fault"]()
            self.assertFalse(path.exists())

    def test_disabled_status_once_is_unknown_off_and_non_success(self):
        args = SimpleNamespace(config="mock://status", once=True, off=False)
        settings = SimpleNamespace(enabled=False, check_seconds=30)
        parser = SimpleNamespace(add_argument=lambda *a, **k: None, parse_args=lambda: args)
        effects = []
        namespace = {"argparse": SimpleNamespace(ArgumentParser=lambda: parser),
                     "DEFAULT_CONFIG": "mock://status", "STATUS_LED": "mock://led",
                     "settings_from_config": lambda path: settings, "running": True,
                     "os": SimpleNamespace(path=SimpleNamespace(isdir=lambda path: True)),
                     "signal": SimpleNamespace(SIGTERM=15, SIGINT=2, signal=lambda *a: None), "stop": forbidden,
                     "write_status_state": lambda *args: effects.append(args),
                     "set_status": lambda value: effects.append(("status", value)),
                     "set_copy": lambda value: effects.append(("copy", value)),
                     "time": SimpleNamespace(sleep=forbidden)}
        definitions("status_led.py", {"main"}, namespace)
        self.assertEqual(namespace["main"](), 1)
        self.assertEqual(effects[0][0], "unknown")
        self.assertEqual(effects[-2:], [("status", 0), ("copy", 0)])

    def test_metrics_slot_map_is_operator_supplied_not_a_hardcoded_inventory(self):
        namespace = {"os": SimpleNamespace(environ={}), "json": json}
        definitions("metrics.py", {"disk_slot_map"}, namespace)
        self.assertEqual(namespace["disk_slot_map"](), {})
        namespace["os"].environ["PROXNAP_METRICS_SLOT_MAP"] = '{"fixture-identity":"hdd1"}'
        self.assertEqual(namespace["disk_slot_map"](), {"fixture-identity": "hdd1"})
        namespace["os"].environ["PROXNAP_METRICS_SLOT_MAP"] = "[]"
        with self.assertRaises(ValueError):
            namespace["disk_slot_map"]()


# Direct execution guard follows every test class.

class LivePayloadRefusalTests(unittest.TestCase):
    def namespace(self, uid=0, model=True, state="inactive", rc=3):
        tree = ast.parse((ROOT / "scripts/install_payload.py").read_text())
        nodes: list[ast.stmt] = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "live_preflight"]
        self.assertEqual(len(nodes), 1)
        calls = []
        namespace = {"os": SimpleNamespace(geteuid=lambda: uid),
                     "require_supported_model": lambda: model,
                     "subprocess": SimpleNamespace(run=lambda argv, **kwargs: calls.append(argv) or
                         SimpleNamespace(stdout=state, stderr="", returncode=rc))}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "<live-preflight-only>", "exec"), namespace)
        return namespace, calls

    def test_nonroot_refused_before_service_query(self):
        ns, calls = self.namespace(uid=1000)
        with self.assertRaises(ValueError):
            ns["live_preflight"]()
        self.assertEqual(calls, [])

    def test_wrong_chassis_refused_before_service_query(self):
        ns, calls = self.namespace(model=False)
        with self.assertRaises(ValueError):
            ns["live_preflight"]()
        self.assertEqual(calls, [])

    def test_active_failed_and_transitional_states_refused(self):
        for state in ("active", "activating", "deactivating", "failed", "reloading", "future-token"):
            with self.subTest(state=state):
                ns, calls = self.namespace(state=state, rc=0)
                with self.assertRaises(ValueError):
                    ns["live_preflight"]()
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][:2], ["systemctl", "is-active"])

    def test_only_verified_inactive_services_accepted(self):
        ns, calls = self.namespace()
        ns["live_preflight"]()
        self.assertGreater(len(calls), 0)
        self.assertTrue(all(argv[:2] == ["systemctl", "is-active"] for argv in calls))

class UninstallFailureBoundaryTests(unittest.TestCase):
    def exercise(self, *, chassis=0, ventilation=0):
        text = (ROOT / "scripts/install.sh").read_text()
        body = "uninstall_all() {" + text.split("uninstall_all() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
        self.assertNotIn("/usr/bin/python", body)
        self.assertIn("verify_uninstall_chassis", body)
        self.assertIn("request_full_speed", body)
        with tempfile.TemporaryDirectory(prefix="proxnap-uninstall-", dir=os.environ["TMPDIR"]) as temp:
            log = Path(temp) / "calls"
            # Only the literal function is loaded, without production helper
            # definitions. Every external/final effect is a mandatory inert
            # function here, not a PATH alias or production default fallback.
            script = (
                'set -eu\ndry_run=false\npurge=false\n'
                'verify_uninstall_chassis() { printf "chassis\\n" >> "$CALLS"; return ' + str(chassis) + '; }\n'
                'request_full_speed() { printf "ventilation\\n" >> "$CALLS"; return ' + str(ventilation) + '; }\n'
                'systemctl() { printf "service %s\\n" "$*" >> "$CALLS"; }\n'
                'run() { printf "mock %s\\n" "$*" >> "$CALLS"; }\n'
                + body + 'uninstall_all\n'
            )
            result = subprocess.run(["bash", "-c", script], env={**os.environ, "CALLS": str(log)},
                                    capture_output=True, text=True, timeout=10)
            return result, log.read_text().splitlines() if log.exists() else []

    def test_wrong_chassis_has_no_service_or_removal_effect(self):
        result, calls = self.exercise(chassis=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, ["chassis"])

    def test_failed_ventilation_retains_code_and_units(self):
        result, calls = self.exercise(ventilation=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ventilation", calls)
        self.assertFalse(any(x.startswith("mock ") for x in calls))
        self.assertIn("code and units retained", result.stderr)

    def test_successful_mock_ventilation_precedes_mock_removal(self):
        result, calls = self.exercise()
        self.assertEqual(result.returncode, 0, result.stderr)
        remove = next(i for i, value in enumerate(calls) if value.startswith("mock rm "))
        self.assertLess(calls.index("ventilation"), remove)


if __name__ == "__main__":
    unittest.main()
