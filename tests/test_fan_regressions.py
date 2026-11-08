"""Offline stdlib tests: AST-selected source, fake hwmon, real local flock/replace.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -v
No root, pip, SSH, private evidence, real sysfs or service is used. Each test
creates disposable files beneath the explicit runtime scratch directory and removes them itself.
The candidate .conf is read-only; altered policies are generated in fixtures.
"""
import contextlib
from offline_loader import load_source
import io
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / "src/proxnap/fan_control.py"
CANDIDATE = HERE.parent / "configs/fan-control.conf"
sys.dont_write_bytecode = True


class OfflineCase(unittest.TestCase):
    def setUp(self):
        name = "offline_proxnap_fans_" + self.id().replace(".", "_")
        self.m = load_source(SOURCE, name)
        self.addCleanup(sys.modules.pop, name, None)
        self.temp = tempfile.TemporaryDirectory(prefix="offline-fan-", dir=os.environ["TMPDIR"])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hw = self.root / "fakehwmon"
        self.hw.mkdir()
        self.qnap = self.device("hwmon0", "qnap8528", {
            "pwm7": "127", "pwm1": "102", "temp1_input": "55000",
            "temp6_input": "35000", "temp7_input": "36000", "temp8_input": "37000",
            "fan1_input": "1234", "fan7_input": "2345",
        })
        self.core = self.device("hwmon1", "coretemp", {
            "temp1_label": "Package id 0", "temp1_input": "50000",
        })
        self.disk = self.device("hwmon2", "drivetemp", {"temp1_input": "40000"})
        self.cfg = self.root / "fan-control.conf"
        self.raw = self.m.read_config(str(CANDIDATE))
        self.config()
        self.clock = 1000.0
        self.writes = []
        self.reads = []
        self.globs = []
        self.lock = self.root / "actuation.lock"
        self.real_write = self.m.write_file
        original_read = self.m.read_file
        original_glob = self.m.glob.glob

        def guarded_read(path, default=""):
            if not Path(path).is_relative_to(self.root):
                raise AssertionError("test attempted non-fixture hwmon read: " + str(path))
            self.reads.append(str(path))
            return original_read(path, default)

        def guarded_glob(pattern):
            if not Path(pattern).is_relative_to(self.root):
                raise AssertionError("test attempted non-fixture discovery: " + str(pattern))
            self.globs.append(str(pattern))
            return original_glob(pattern)

        for obj, attr, value in (
            (self.m, "HWMON_ROOT", str(self.hw)),
            (self.m, "ACTUATION_LOCK", str(self.lock)),
            (self.m, "read_file", guarded_read),
            (self.m.glob, "glob", guarded_glob),
            (self.m, "write_file", mock.Mock(side_effect=self.ec_write)),
            (self.m.time, "monotonic", lambda: self.clock),
            (self.m.signal, "signal", mock.Mock()),
        ):
            patcher = mock.patch.object(obj, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def device(self, directory, name, files):
        path = self.hw / directory
        path.mkdir()
        (path / "name").write_text(name, encoding="utf-8")
        for filename, value in files.items():
            (path / filename).write_text(value, encoding="ascii")
        return path

    def config(self, **changes):
        raw = self.raw | {key: str(value) for key, value in changes.items()}
        self.cfg.write_text("".join(f"{key}={value}\n" for key, value in raw.items()), encoding="utf-8")
        return raw

    def settings(self, **changes):
        raw = self.raw | {key: str(value) for key, value in changes.items()}
        return self.m.settings_from_raw(raw)

    def ec_write(self, path, value):
        path = Path(path)
        if not path.is_relative_to(self.hw) or path.name not in {"pwm1", "pwm7"}:
            raise AssertionError("test attempted unsafe write: " + str(path))
        if not path.is_file():
            return False
        raw = int(value)
        self.writes.append((str(path), raw))
        # Independent fake EC: raw -> integer percent -> exported raw.
        path.write_text(str((raw * 100 // 255) * 255 // 100), encoding="ascii")
        return True

    def main(self, *arguments):
        return self.m.main(["--config", str(self.cfg), *arguments])

    def cpu_writes(self):
        return [raw for path, raw in self.writes if Path(path).name == "pwm7"]

    def rear_writes(self):
        return [raw for path, raw in self.writes if Path(path).name == "pwm1"]

    def subprocess_cli(self, *arguments):
        # Only substitute fixture roots. Main, file IO, flock, config parsing,
        # readback and exit codes are the unmocked source in another process.
        bootstrap = (
            "import sys;sys.path.insert(0,sys.argv[1]);from offline_loader import load_source; "
            "m=load_source(sys.argv[2], 'offline_child'); "
            "m.HWMON_ROOT=sys.argv[3];m.ACTUATION_LOCK=sys.argv[4]; "
            "m.signal.signal=lambda *args:None; "
            "raise SystemExit(m.main(['--config',sys.argv[5],*sys.argv[6:]]))"
        )
        return subprocess.run([sys.executable, "-B", "-c", bootstrap, str(HERE), str(SOURCE),
                               str(self.hw), str(self.lock), str(self.cfg), *arguments],
                              capture_output=True, text=True, timeout=10)

    def loop(self, steps):
        steps = iter(steps)

        def sleep(_seconds):
            self.clock += _seconds
            try:
                next(steps)()
            except StopIteration:
                self.m.stop()

        with mock.patch.object(self.m.time, "sleep", side_effect=sleep):
            return self.main()


class ConfigurationTests(OfflineCase):
    def test_sensor_policy_must_cover_the_group_thermal_domain(self):
        for policy in ("hdd", "storage", "storage_passive", "system"):
            with self.subTest(cpu=policy), self.assertRaises(ValueError):
                self.settings(cpu_sensor_policy=policy)
        for policy in ("cpu_package", "system"):
            with self.subTest(rear=policy), self.assertRaises(ValueError):
                self.settings(rear_sensor_policy=policy)
        for cpu in ("cpu_package", "all"):
            for rear in ("hdd", "storage", "storage_passive", "all"):
                with self.subTest(cpu=cpu, rear=rear):
                    self.settings(cpu_sensor_policy=cpu, rear_sensor_policy=rear)

    def test_candidate_exact_policy_is_consumed_readonly(self):
        before = CANDIDATE.stat()
        candidate = self.m.settings_from_config(str(CANDIDATE))
        self.assertEqual(candidate.groups[0].curve,
                         [(0, 50), (65, 50), (70, 55), (75, 65), (80, 80), (85, 90), (90, 100)])
        self.assertEqual(candidate.cpu_min_decrease_interval, 30)
        self.assertEqual(candidate.rear_min_decrease_interval, 120)
        self.assertEqual(candidate.cpu_max_duty_step, 5)
        self.assertEqual(candidate.rear_max_duty_step, 8)
        self.assertEqual(candidate.stop_duty, 100)
        self.assertEqual([g.pwm for g in candidate.groups], ["pwm7", "pwm1"])
        self.assertEqual([g.min_duty for g in candidate.groups], [50, 40])
        self.assertEqual(CANDIDATE.stat().st_mtime_ns, before.st_mtime_ns)

    def test_all_numeric_fields_reject_nan_and_infinity(self):
        optional = {"cpu_manual_duty", "rear_manual_duty"}
        numbers = [key for key, value in self.raw.items()
                   if value.replace(".", "", 1).isdigit() or key in optional]
        self.assertGreater(len(numbers), 20)
        for key in numbers:
            for value in ("nan", "inf", "-inf", "not-a-number"):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.settings(**{key: value})
        for mode in ("quiet", "normal", "extra", "full"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.settings(mode=mode, cpu_manual_duty="NaN")

    def test_paths_duplicates_and_physical_swaps_are_rejected(self):
        invalid = [
            {"cpu_pwm": "/tmp/unsafe"}, {"rear_pwm": "../pwm1"},
            {"cpu_pwm": "pwm1"}, {"rear_pwm": "pwm7"},
            {"cpu_pwm": "pwm1", "rear_pwm": "pwm7"}, {"cpu_pwm": "pwm7/../pwm7"},
        ]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.settings(**changes)
        with self.assertRaises(ValueError):
            self.m.parse_config_text("mode=auto\nmode=full\n")
        with self.assertRaises(ValueError):
            self.m.parse_config_text("invalid line\n")

    def test_invalid_ranges_and_unknown_keys_do_not_silently_clamp(self):
        for changes in (
            {"cpu_min_duty": 20}, {"rear_min_duty": 20},
            {"cpu_min_duty": 90, "cpu_max_duty": 80},
            {"interval_seconds": 0}, {"cpu_temp_ema_alpha": 0},
            {"rear_min_decrease_interval": -1}, {"rear_manual_duty": 101},
            {"cpu_emergency_temp": 93}, {"rear_emergency_temp": 66},
            {"cpu_critical_temp": 100}, {"enabled": "maybe"},
            {"mode": "invalid"}, {"cpu_sensor_policy": "nvme"}, {"typo": 5},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.settings(**changes)

    def test_curves_must_be_finite_ordered_monotonic_and_full_endpoint(self):
        for curve in ("0:50,NaN:100", "0:NaN,90:100", "0:50,90:inf", "0:50",
                      "0:50,bad,90:100", "0:50,0:60,90:100", "90:100,0:50",
                      "0:50,60:40,90:100", "0:50,90:99", "0:50,93:100"):
            with self.subTest(curve=curve), self.assertRaises(ValueError):
                self.settings(cpu_curve=curve)

    def test_profile_floors_and_endpoint_protection(self):
        for mode in self.m.VALID_MODES:
            settings = self.settings(mode=mode)
            for group in settings.groups:
                with self.subTest(mode=mode, group=group.name):
                    self.assertGreaterEqual(self.m.duty_for_temp(10, group), 50 if group.name == "cpu" else 40)
                    self.assertEqual(group.curve[-1][1], 100)
                    self.assertEqual(self.m.controlled_duty(group, self.m.emergency_temp(group, settings),
                                                          40, settings, self.m.ControlState(), 0), 100)

    def test_check_config_has_no_lock_hwmon_or_write(self):
        original = self.cfg.read_bytes()
        with mock.patch.object(self.m, "exclusive_lock", side_effect=AssertionError("lock")), \
                mock.patch.object(self.m, "qnap_hwmon", side_effect=AssertionError("hwmon")):
            self.assertEqual(self.main("--check-config"), 0)
            self.config(interval_seconds="NaN")
            self.assertEqual(self.main("--check-config"), 1)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.reads, [])
        self.assertFalse(self.lock.exists())
        self.assertIn(b"enabled=true", original)

    def test_real_cli_check_config_and_invalid_exit(self):
        good = self.subprocess_cli("--check-config")
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        self.assertIn("no hardware IO", good.stdout)
        self.config(cpu_pwm="pwm1")
        bad = self.subprocess_cli("--check-config")
        self.assertEqual(bad.returncode, 1)
        self.assertFalse(self.lock.exists())


class ThermalTests(OfflineCase):
    def test_manual_low_duty_cannot_override_missing_critical_or_emergency(self):
        settings = self.settings(cpu_manual_duty=20, rear_manual_duty=20)
        for group in settings.groups:
            group.max_duty = 70
            for temperature in (None, group.critical_temp, self.m.emergency_temp(group, settings), float("nan")):
                with self.subTest(group=group.name, temp=temperature):
                    self.assertEqual(self.m.controlled_duty(group, temperature, 20, settings,
                                                          self.m.ControlState(applied_duty=50), 0), 100)

    def test_manual_low_duty_clamps_to_hardware_floor_when_cool(self):
        settings = self.settings(cpu_manual_duty=20, rear_manual_duty=20)
        for group, expected in zip(settings.groups, (50, 40)):
            self.assertEqual(self.m.controlled_duty(group, 30, 20, settings, self.m.ControlState(), 0), expected)
        self.config(cpu_manual_duty=20, rear_manual_duty=20)
        self.assertEqual(self.main("--once"), 0)
        self.assertEqual(self.cpu_writes(), [128])
        self.assertEqual(self.rear_writes(), [102])

    def test_increase_bypasses_hold_step_and_deadband(self):
        settings = self.settings(cpu_min_increase_interval=1000, cpu_max_duty_step=1, duty_hysteresis=30)
        group = settings.groups[0]
        state = self.m.ControlState(applied_duty=50, last_change=self.clock)
        self.assertEqual(self.m.controlled_duty(group, 85, 90, settings, state, self.clock + 1), 90)
        self.assertEqual(state.applied_duty, 50)  # proposed != confirmed
        self.assertEqual(self.m.controlled_duty(group, 66, 51, settings, state, self.clock + 1), 51)

    def test_raw_89_with_low_average_never_requests_below_raw_curve(self):
        settings = self.settings()
        group = settings.groups[0]
        state = self.m.ControlState(applied_duty=90, last_change=self.clock,
                                   temp_samples=[(self.clock - i, 40) for i in range(1, 30)])
        average = self.m.smoothed_temp(group, 89, settings, state, self.clock)
        self.assertLess(average, 45)
        target = max(self.m.duty_for_temp(89, group), self.m.duty_for_temp(average, group))
        self.assertEqual(target, 98)
        duty = self.m.controlled_duty(group, 89, target, settings, state, self.clock)
        self.assertEqual(duty, 98)
        self.assertTrue(self.m.maybe_set_group_duty(str(self.qnap), group, duty, state, self.clock))
        self.assertEqual(self.cpu_writes(), [250])

    def test_main_uses_raw_requirement_even_with_low_ema(self):
        self.config(cpu_temp_average_seconds=0, cpu_temp_ema_alpha=0.05)

        def heat():
            (self.core / "temp1_input").write_text("89000")

        self.assertEqual(self.loop([heat]), 0)
        self.assertEqual(self.cpu_writes()[:2], [128, 250])

    def test_high_smoothed_temp_can_hold_cooling_above_raw(self):
        settings = self.settings()
        group = settings.groups[0]
        state = self.m.ControlState(temp_samples=[(self.clock - 1, 90)], applied_duty=100)
        smoothed = self.m.smoothed_temp(group, 50, settings, state, self.clock)
        target = max(self.m.duty_for_temp(50, group), self.m.duty_for_temp(smoothed, group))
        self.assertEqual(smoothed, 70)
        self.assertEqual(target, 55)
        self.assertGreater(target, self.m.duty_for_temp(50, group))

    def test_descent_holds_steps_and_converges_to_floor(self):
        settings = self.settings()
        group = settings.groups[0]
        state = self.m.ControlState(applied_duty=90, last_change=0)
        seen = []
        for now in range(0, 301, 5):
            duty = self.m.controlled_duty(group, 40, 50, settings, state, now)
            if now < 30:
                self.assertEqual(duty, 90)
            previous = state.applied_duty
            self.assertTrue(self.m.maybe_set_group_duty(str(self.qnap), group, duty, state, now))
            self.assertLessEqual(previous - duty, 5)
            seen.append(duty)
        self.assertEqual(seen[-1], 50)
        self.assertEqual(sorted(set(seen), reverse=True), list(range(90, 49, -5)))

    def test_final_decrease_smaller_than_deadband_converges(self):
        settings = self.settings()
        group = settings.groups[0]
        state = self.m.ControlState(applied_duty=53, last_change=0)
        self.assertEqual(self.m.controlled_duty(group, 40, 50, settings, state, 29), 53)
        duty = self.m.controlled_duty(group, 40, 50, settings, state, 30)
        self.assertEqual(duty, 50)
        self.assertTrue(self.m.maybe_set_group_duty(str(self.qnap), group, duty, state, 30))
        self.assertEqual(state.applied_duty, 50)

    def test_restart_baseline_prevents_abrupt_descent(self):
        (self.qnap / "pwm7").write_text("255")
        (self.qnap / "pwm1").write_text("255")
        self.assertEqual(self.main("--once"), 0)
        self.assertEqual(self.cpu_writes(), [255])
        self.assertEqual(self.rear_writes(), [255])

    def test_unreadable_starting_baseline_is_conservative(self):
        (self.qnap / "pwm7").write_text("invalid")
        self.assertEqual(self.main("--once"), 1)
        self.assertEqual(self.cpu_writes(), [255])
        self.assertEqual(self.rear_writes(), [255])


class PwmTests(OfflineCase):
    def test_raw_140_reads_137_and_cache_skips_io_until_refresh(self):
        group = self.settings().groups[0]
        state = self.m.ControlState()
        duty = 140 * 100 / 255
        with mock.patch.object(self.m, "read_pwm", wraps=self.m.read_pwm) as read:
            self.assertTrue(self.m.maybe_set_group_duty(str(self.qnap), group, duty, state, 100, 60))
            self.assertEqual(state.last_pwm, 140)
            self.assertEqual(state.readback_pwm, 137)
            self.assertTrue(state.write_attempted)
            self.assertEqual(read.call_count, 1)
            self.assertTrue(self.m.maybe_set_group_duty(str(self.qnap), group, duty, state, 159, 60))
            self.assertFalse(state.write_attempted)
            self.assertEqual(read.call_count, 1)
            self.assertTrue(self.m.maybe_set_group_duty(str(self.qnap), group, duty, state, 160, 60))
            self.assertTrue(state.write_attempted)
            self.assertEqual(read.call_count, 2)
        self.assertEqual(self.cpu_writes(), [140, 140])

    def test_write_failure_never_confirms_cache_or_state_and_retries(self):
        group = self.settings().groups[0]
        state = self.m.ControlState(applied_duty=90, last_change=1)
        with mock.patch.object(self.m, "write_file", return_value=False), \
                mock.patch.object(self.m, "read_pwm", side_effect=AssertionError("read after failed write")):
            self.assertFalse(self.m.maybe_set_group_duty(str(self.qnap), group, 60, state, 100))
        self.assertIsNone(state.last_pwm)
        self.assertIsNone(state.readback_pwm)
        self.assertEqual(state.applied_duty, 90)
        self.assertEqual(state.last_change, 1)
        self.assertTrue(self.m.maybe_set_group_duty(str(self.qnap), group, 60, state, 101))
        self.assertEqual(state.applied_duty, 60)
        self.assertEqual(self.cpu_writes(), [153])
        self.assertIn("ERROR: PWM", self.output.getvalue())

    def test_readback_failure_or_mismatch_never_confirms_and_retries(self):
        group = self.settings().groups[0]
        for readback in (None, 140, 255, -1):
            state = self.m.ControlState(applied_duty=90, last_change=1)
            with self.subTest(readback=readback), mock.patch.object(self.m, "read_pwm", return_value=readback):
                self.assertFalse(self.m.maybe_set_group_duty(str(self.qnap), group, 140 * 100 / 255, state, 100))
            self.assertIsNone(state.last_pwm)
            self.assertEqual(state.applied_duty, 90)
            self.assertTrue(self.m.maybe_set_group_duty(str(self.qnap), group, 140 * 100 / 255, state, 101))

    def test_low_level_write_file_reports_real_fakehwmon_io(self):
        path = self.qnap / "pwm7"
        self.assertTrue(self.real_write(str(path), "255"))
        self.assertEqual(path.read_text(), "255")
        self.assertFalse(self.real_write(str(self.root / "missing-parent" / "pwm7"), "255"))

    def test_equivalent_raw_command_still_converges_logical_duty(self):
        group = self.settings().groups[0]
        state = self.m.ControlState()
        self.assertTrue(self.m.maybe_set_group_duty(str(self.qnap), group, 50.1, state, 100))
        self.assertTrue(self.m.maybe_set_group_duty(str(self.qnap), group, 50, state, 130))
        self.assertEqual(state.applied_duty, 50)
        self.assertEqual(self.cpu_writes(), [128])

    def test_read_pwm_rejects_invalid_raw(self):
        path = self.qnap / "pwm7"
        for value in ("", "nan", "-1", "256", "50.0"):
            path.write_text(value)
            with self.subTest(value=value):
                self.assertIsNone(self.m.read_pwm(str(path)))

    def test_remap_same_command_must_write_new_destination(self):
        group = self.settings().groups[0]
        state = self.m.ControlState()
        self.assertTrue(self.m.maybe_set_group_duty(str(self.qnap), group, 60, state, 100))
        new = self.device("hwmon8", "fake-unused", {"pwm7": "255", "pwm1": "255"})
        self.assertTrue(self.m.maybe_set_group_duty(str(new), group, 60, state, 101))
        self.assertEqual([path for path, _ in self.writes], [str(self.qnap / "pwm7"), str(new / "pwm7")])

    def test_no_rpm_or_extra_pwm_polling_in_steady_state(self):
        self.assertEqual(self.loop([lambda: None]), 0)
        fan_reads = [path for path in self.reads if Path(path).name.startswith("fan")]
        self.assertEqual(fan_reads, [])
        # Startup baseline + initial write readback + stop readback, not loop polling.
        for pwm in ("pwm7", "pwm1"):
            self.assertEqual(self.reads.count(str(self.qnap / pwm)), 3)


class SensorTests(OfflineCase):
    def test_all_rejects_missing_primary_label_before_first_inventory(self):
        (self.core / "temp1_label").unlink()
        (self.core / "temp1_input").write_text("100000")
        self.assertIsNone(self.m.policy_temp("all"))
        self.config(cpu_sensor_policy="all")
        self.assertEqual(self.main("--once"), 1)
        self.assertEqual(set(self.cpu_writes()), {255})
        self.assertEqual(self.rear_writes()[-1], 255)
        self.assertIn("requires an identifiable CPU package", self.output.getvalue())

    def test_all_rejects_unknown_primary_label_before_first_inventory(self):
        (self.core / "temp1_label").write_text("unknown")
        (self.core / "temp1_input").write_text("100000")
        self.config(cpu_sensor_policy="all")
        self.assertEqual(self.main("--once"), 1)
        self.assertEqual(set(self.cpu_writes()), {255})
        self.assertIn("sensor unavailable", self.output.getvalue())

    def test_all_does_not_claim_cpu_coverage_without_a_primary_device(self):
        shutil.rmtree(self.core)
        self.assertIsNone(self.m.policy_temp("all"))
        # Default policy retains its independently specified startup fallback.
        self.assertEqual(self.m.policy_temp("cpu_package"), 55)

    def test_partial_disk_path_disappearance_is_not_hidden_by_max(self):
        second = self.device("hwmon3", "drivetemp", {"temp1_input": "42000"})
        self.assertEqual(self.m.policy_temp("hdd"), 42)
        (second / "temp1_input").unlink()
        self.assertIsNone(self.m.policy_temp("hdd"))
        self.assertIn("sensor loss/read failure", self.output.getvalue())
        (second / "temp1_input").write_text("41000")
        self.assertEqual(self.m.policy_temp("hdd"), 41)

    def test_partial_device_removal_and_refresh_remains_missing(self):
        second = self.device("hwmon3", "drivetemp", {"temp1_input": "42000"})
        self.assertEqual(self.m.policy_temp("hdd"), 42)
        shutil.rmtree(second)
        self.m.hwmon_cache_until = 0
        self.assertIsNone(self.m.policy_temp("hdd"))
        self.assertIn(str(second / "temp1_input"), self.output.getvalue())

    def test_partial_unreadable_sensor_is_not_ignored(self):
        second = self.device("hwmon3", "drivetemp", {"temp1_input": "42000"})
        self.assertEqual(self.m.policy_temp("hdd"), 42)
        (second / "temp1_input").write_text("error")
        self.assertIsNone(self.m.policy_temp("hdd"))

    def test_coretemp_disappearance_cannot_fallback_to_valid_ec(self):
        self.assertEqual(self.m.policy_temp("cpu_package"), 50)
        shutil.rmtree(self.core)
        self.m.hwmon_cache_until = 0
        self.assertIsNone(self.m.policy_temp("cpu_package"))
        self.assertNotIn(str(self.qnap / "temp1_input"), self.reads)

    def test_coretemp_discovered_but_unreadable_cannot_fallback(self):
        (self.core / "temp1_input").write_text("bad")
        self.assertIsNone(self.m.policy_temp("cpu_package"))
        self.assertNotIn(str(self.qnap / "temp1_input"), self.reads)

    def test_coretemp_unreadable_labels_cannot_enable_fallback(self):
        (self.core / "temp1_label").write_text("unknown")
        self.assertIsNone(self.m.policy_temp("cpu_package"))
        self.assertNotIn(str(self.qnap / "temp1_input"), self.reads)

    def test_cpu_ec_fallback_only_before_any_primary_seen(self):
        shutil.rmtree(self.core)
        self.assertEqual(self.m.policy_temp("cpu_package"), 55)
        self.core = self.device("hwmon1", "coretemp", {
            "temp1_label": "Package id 0", "temp1_input": "60000"})
        self.m.hwmon_cache_until = 0
        self.assertEqual(self.m.policy_temp("cpu_package"), 60)
        shutil.rmtree(self.core)
        self.m.hwmon_cache_until = 0
        self.assertIsNone(self.m.policy_temp("cpu_package"))

    def test_partial_system_and_all_sensor_loss(self):
        self.assertEqual(self.m.policy_temp("system"), 37)
        self.assertEqual(self.m.policy_temp("all"), 50)
        (self.qnap / "temp7_input").unlink()
        self.assertIsNone(self.m.policy_temp("system"))
        self.assertIsNone(self.m.policy_temp("all"))
        self.assertIsNone(self.m.policy_temp("storage"))

    def test_partial_loss_cannot_be_hidden_by_overlapping_policy_reload(self):
        second = self.device("hwmon3", "drivetemp", {"temp1_input": "42000"})
        self.assertEqual(self.m.policy_temp("hdd"), 42)
        (second / "temp1_input").unlink()
        for policy in ("storage", "storage_passive", "all"):
            with self.subTest(policy=policy):
                self.assertIsNone(self.m.policy_temp(policy))

    def test_observed_coretemp_loss_is_required_by_all_policy_too(self):
        self.assertEqual(self.m.policy_temp("cpu_package"), 50)
        shutil.rmtree(self.core)
        self.m.hwmon_cache_until = 0
        self.assertIsNone(self.m.policy_temp("all"))

    def test_no_nvme_temperature_polling_in_any_policy(self):
        nvme = self.device("hwmon4", "nvme", {"temp1_input": "99000", "temp1_label": "Composite"})
        for policy in self.m.VALID_POLICIES:
            with self.subTest(policy=policy):
                self.assertIsNotNone(self.m.policy_temp(policy))
        self.assertNotIn(str(nvme / "temp1_input"), self.reads)
        self.assertNotIn(str(nvme / "temp1_label"), self.reads)

    def test_inventory_is_retained_across_config_reload(self):
        second = self.device("hwmon3", "drivetemp", {"temp1_input": "42000"})

        def remove_and_reload():
            (second / "temp1_input").unlink()
            self.config(mode="quiet")

        self.assertEqual(self.loop([remove_and_reload]), 0)  # stop full-duty succeeds
        self.assertEqual(self.rear_writes()[-2:], [255, 255])
        self.assertIn(str(second / "temp1_input"), self.output.getvalue())
        self.assertIn("sensor unavailable", self.output.getvalue())


class RuntimeTests(OfflineCase):
    def test_incoherent_cpu_sensor_policy_is_rejected_and_fails_safe(self):
        self.config(cpu_sensor_policy="hdd")
        (self.core / "temp1_input").write_text("100000")
        self.assertEqual(self.main("--once"), 1)
        self.assertEqual(self.cpu_writes(), [255])
        self.assertEqual(self.rear_writes(), [255])
        self.assertIn("does not cover the cpu thermal domain", self.output.getvalue())

    def test_stop_during_once_still_requests_full_duty_before_exit(self):
        first = True

        def stop_during_write(path, value):
            nonlocal first
            result = self.ec_write(path, value)
            if first:
                first = False
                self.m.stop()
            return result

        with mock.patch.object(self.m, "write_file", side_effect=stop_during_write):
            self.assertEqual(self.main("--once"), 0)
        self.assertEqual(self.cpu_writes(), [128, 255])
        self.assertEqual(self.rear_writes(), [102, 255])

    def test_disabled_once_terminates_and_sets_both_full(self):
        self.config(enabled="false", stop_duty=20)
        with mock.patch.object(self.m.time, "sleep", side_effect=AssertionError("once slept")):
            self.assertEqual(self.main("--once"), 0)
        self.assertEqual(self.cpu_writes(), [255])
        self.assertEqual(self.rear_writes(), [255])

    def test_safe_ignores_invalid_config_and_uses_fixed_mapping(self):
        self.config(cpu_pwm="../../untrusted", stop_duty=20)
        with mock.patch.object(self.m, "settings_from_config", side_effect=AssertionError("safe read config")):
            self.assertEqual(self.main("--safe"), 0)
        self.assertEqual(self.cpu_writes(), [255])
        self.assertEqual(self.rear_writes(), [255])

    def test_safe_write_failure_is_nonzero_and_other_zone_still_attempted(self):
        original = self.ec_write

        def fail_cpu(path, value):
            if Path(path).name == "pwm7":
                return False
            return original(path, value)

        with mock.patch.object(self.m, "write_file", side_effect=fail_cpu):
            self.assertEqual(self.main("--safe"), 1)
        self.assertEqual(self.rear_writes(), [255])
        self.assertIn("write_ok=False", self.output.getvalue())

    def test_safe_readback_failure_is_nonzero_and_other_zone_attempted(self):
        original = self.m.read_pwm
        with mock.patch.object(self.m, "read_pwm", side_effect=lambda path: None if Path(path).name == "pwm7" else original(path)):
            self.assertEqual(self.main("--safe"), 1)
        self.assertEqual(self.cpu_writes(), [255])
        self.assertEqual(self.rear_writes(), [255])

    def test_real_subprocess_safe_and_disabled_once_exit_success(self):
        safe = self.subprocess_cli("--safe")
        self.assertEqual(safe.returncode, 0, safe.stdout + safe.stderr)
        self.assertEqual((self.qnap / "pwm7").read_text(), "255")
        self.assertEqual((self.qnap / "pwm1").read_text(), "255")
        self.config(enabled="false", stop_duty=20)
        disabled = self.subprocess_cli("--once")
        self.assertEqual(disabled.returncode, 0, disabled.stdout + disabled.stderr)

    def test_real_subprocess_full_once_and_safe_failure_exit(self):
        self.config(mode="full")
        once = self.subprocess_cli("--once")
        self.assertEqual(once.returncode, 0, once.stdout + once.stderr)
        self.assertEqual((self.qnap / "pwm7").read_text(), "255")
        path = self.qnap / "pwm7"
        path.unlink()
        path.mkdir()  # real IsADirectoryError, not root-dependent permissions
        failed = self.subprocess_cli("--safe")
        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        self.assertIn("write_ok=False", failed.stdout)
        self.assertEqual((self.qnap / "pwm1").read_text(), "255")

    def test_real_subprocess_lock_conflict_has_no_hardware_io(self):
        before = [(self.qnap / pwm).read_bytes() for pwm in ("pwm7", "pwm1")]
        with self.m.exclusive_lock(str(self.lock)):
            blocked = self.subprocess_cli("--safe")
        self.assertEqual(blocked.returncode, 1, blocked.stdout + blocked.stderr)
        self.assertIn("lock held", blocked.stdout)
        self.assertEqual([(self.qnap / pwm).read_bytes() for pwm in ("pwm7", "pwm1")], before)

    def test_symlink_actuator_lock_refuses_all_hardware_io(self):
        target = self.root / "existing-lock-target"
        target.write_text("untouched")
        self.lock.symlink_to(target)
        self.assertEqual(self.main("--safe"), 1)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.reads, [])
        self.assertEqual(target.read_text(), "untouched")

    def test_safe_exception_in_one_zone_does_not_skip_other_zone(self):
        def failure(path, value):
            if Path(path).name == "pwm7":
                raise OSError("injected zone exception")
            return self.ec_write(path, value)

        with mock.patch.object(self.m, "write_file", side_effect=failure):
            self.assertEqual(self.main("--safe"), 1)
        self.assertEqual(self.rear_writes(), [255])

    def test_safe_missing_hwmon_returns_error(self):
        (self.qnap / "name").write_text("not-qnap")
        self.assertEqual(self.main("--safe"), 1)
        self.assertEqual(self.writes, [])

    def test_held_lock_blocks_once_safe_daemon_and_public_safe_without_io(self):
        with self.m.exclusive_lock(str(self.lock)):
            for arguments in (("--once",), ("--safe",), ()):
                with self.subTest(arguments=arguments):
                    self.assertEqual(self.main(*arguments), 1)
            self.assertEqual(self.m.apply_safe_duty(str(self.cfg)), 1)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.reads, [])
        self.assertEqual(self.globs, [])
        self.assertIn("lock held", self.output.getvalue())

    def test_lock_is_released_after_runtime_exit(self):
        self.assertEqual(self.main("--once"), 0)
        with self.m.exclusive_lock(str(self.lock)):
            pass

    def test_invalid_config_triggers_fixed_full_duty_and_error_exit(self):
        self.config(cpu_pwm="../untrusted", rear_pwm="pwm7", cpu_emergency_temp="NaN")
        self.assertEqual(self.main("--once"), 1)
        self.assertEqual(self.cpu_writes(), [255])
        self.assertEqual(self.rear_writes(), [255])

    def test_missing_config_triggers_fixed_full_duty_and_error_exit(self):
        self.cfg.unlink()
        self.assertEqual(self.main("--once"), 1)
        self.assertEqual(self.cpu_writes(), [255])
        self.assertEqual(self.rear_writes(), [255])

    def test_runtime_exception_triggers_fixed_full_duty_and_error_exit(self):
        with mock.patch.object(self.m, "smoothed_temp", side_effect=RuntimeError("injected runtime fault")):
            self.assertEqual(self.main("--once"), 1)
        self.assertEqual(self.cpu_writes(), [255])
        self.assertEqual(self.rear_writes(), [255])
        self.assertIn("injected runtime fault", self.output.getvalue())

    def test_once_sensor_missing_with_manual_requests_full_and_errors(self):
        self.config(cpu_manual_duty=20)
        (self.core / "temp1_input").write_text("bad")
        self.assertEqual(self.main("--once"), 1)
        self.assertTrue(all(raw == 255 for raw in self.cpu_writes()))
        self.assertEqual(self.rear_writes()[-1], 255)

    def test_once_write_failure_remains_nonzero_even_if_failsafe_recovers(self):
        count = 0

        def once_failure(path, value):
            nonlocal count
            count += 1
            return False if count == 1 else self.ec_write(path, value)

        with mock.patch.object(self.m, "write_file", side_effect=once_failure):
            self.assertEqual(self.main("--once"), 1)
        self.assertEqual(self.cpu_writes()[-1], 255)
        self.assertEqual(self.rear_writes()[-1], 255)

    def test_daemon_retries_failed_command_instead_of_cached_success(self):
        first = True

        def one_failure(path, value):
            nonlocal first
            if first and Path(path).name == "pwm7":
                first = False
                return False
            return self.ec_write(path, value)

        with mock.patch.object(self.m, "write_file", side_effect=one_failure):
            self.assertEqual(self.loop([lambda: None]), 0)
        # Recovery, next iteration baseline full duty, then full-duty stop.
        self.assertEqual(self.cpu_writes(), [255, 255, 255])

    def test_disable_reenable_resets_baseline_and_command_cache(self):
        self.assertEqual(self.loop([lambda: self.config(enabled="false"), lambda: self.config(enabled="true")]), 0)
        self.assertEqual(self.cpu_writes(), [128, 255, 255, 255])
        self.assertEqual(self.rear_writes(), [102, 255, 255, 255])

    def test_mode_and_nonmode_reload_reset_baseline_and_cache(self):
        for changes in ({"mode": "normal"}, {"cpu_max_duty_step": 2}):
            with self.subTest(changes=changes):
                self.config()
                self.writes.clear()
                self.m.policy_temp_cache.clear()
                (self.qnap / "pwm7").write_text("127")

                def reload():
                    (self.qnap / "pwm7").write_text("255")
                    self.config(**changes)

                self.assertEqual(self.loop([reload]), 0)
                self.assertEqual(self.cpu_writes()[:2], [128, 255])

    def test_hwmon_remap_resets_cache_and_reloads_baseline(self):
        old = self.qnap
        new = self.hw / "hwmon9"

        def remap():
            old.rename(new)
            (new / "pwm7").write_text("255")
            self.m.hwmon_cache_until = 0

        self.assertEqual(self.loop([remap]), 0)
        self.assertIn((str(new / "pwm7"), 255), self.writes)
        self.assertEqual(self.cpu_writes()[:2], [128, 255])

    def test_stop_requests_full_both_and_reports_failure(self):
        original = self.ec_write
        stopping = False

        def fail_on_stop(path, value):
            if stopping and Path(path).name == "pwm7":
                return False
            return original(path, value)

        def finish():
            nonlocal stopping
            stopping = True
            self.m.stop()

        with mock.patch.object(self.m, "write_file", side_effect=fail_on_stop):
            self.assertEqual(self.loop([finish]), 1)
        self.assertEqual(self.rear_writes()[-1], 255)


class AtomicModeTests(OfflineCase):
    def test_atomic_edit_preserves_comments_permissions_owner_and_crlf(self):
        original = "# fan policy\r\n  mode = auto  # keep this comment\r\nenabled=true\r\n"
        self.cfg.write_bytes(original.encode())
        self.cfg.chmod(0o640)
        before = self.cfg.stat()
        self.assertEqual(self.main("--set-mode", "quiet"), 0)
        self.assertEqual(self.cfg.read_bytes(), original.replace("auto", "quiet").encode())
        after = self.cfg.stat()
        self.assertNotEqual(before.st_ino, after.st_ino)
        self.assertEqual(stat.S_IMODE(after.st_mode), 0o640)
        self.assertEqual((before.st_uid, before.st_gid), (after.st_uid, after.st_gid))
        self.assertEqual(self.m.settings_from_config(str(self.cfg)).mode, "quiet")
        self.assertEqual(self.writes, [])
        self.assertEqual(self.reads, [])

    def test_atomic_edit_appends_missing_key_without_losing_last_line(self):
        self.cfg.write_text("# comment\nenabled=true")
        self.assertEqual(self.main("--set-mode", "extra"), 0)
        self.assertEqual(self.cfg.read_text(), "# comment\nenabled=true\nmode=extra\n")

    def test_symlink_edit_is_refused_without_touching_target(self):
        target = self.root / "real.conf"
        self.cfg.rename(target)
        before = target.read_bytes()
        self.cfg.symlink_to(target)
        self.assertEqual(self.main("--set-mode", "full"), 1)
        self.assertEqual(target.read_bytes(), before)
        self.assertTrue(self.cfg.is_symlink())
        self.assertFalse(Path(str(self.cfg) + ".lock").exists())

    def test_edit_lock_serializes_writers_and_does_not_lose_update(self):
        before = self.cfg.read_bytes()
        with self.m.exclusive_lock(str(self.cfg) + ".lock"):
            self.assertEqual(self.main("--set-mode", "full"), 1)
        self.assertEqual(self.cfg.read_bytes(), before)
        self.assertEqual(self.main("--set-mode", "full"), 0)
        self.assertEqual(self.m.settings_from_config(str(self.cfg)).mode, "full")

    def test_atomic_replace_failure_preserves_original_and_cleans_staging(self):
        before = self.cfg.read_bytes()
        with mock.patch.object(self.m.os, "replace", side_effect=OSError("injected replace failure")):
            self.assertEqual(self.main("--set-mode", "quiet"), 1)
        self.assertEqual(self.cfg.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".fan-control-*")), [])

    def test_set_mode_does_not_require_actuator_lock_or_touch_hardware(self):
        with self.m.exclusive_lock(str(self.lock)):
            self.assertEqual(self.main("--set-mode", "full"), 0)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.reads, [])


if __name__ == "__main__":
    unittest.main()
