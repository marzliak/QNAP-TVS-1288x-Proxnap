"""Offline AST-only loop tests: no daemon imports or real effect fallbacks.

Only the requested function AST is executed. OS, time, UI, commands and final
power/update effects are mandatory doubles; missing globals fail closed.
"""
import ast
import builtins
from collections import deque
from pathlib import Path
import struct
from types import SimpleNamespace
import fcntl
import os
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "src/proxnap/copy_update.py"
EVENT = struct.Struct("llHHi")


def settings_from(source):
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Settings")
    values = {}
    for node in cls.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            values[node.target.id] = 0x101 if isinstance(node.value, ast.Name) else ast.literal_eval(node.value)
    # Test-owned identifiers only: never let production paths reach a double.
    for key in ("button_input", "lock_file", "running_flag", "log_file", "lcd_port", "lcd_lock_file"):
        values[key] = "mock://" + key
    return SimpleNamespace(**values)


def packet(at, value, event_type=1, code=0x101):
    sec = int(at)
    usec = round((at - sec) * 1_000_000)
    return EVENT.pack(sec, usec, event_type, code, value)


def clicks(start, count, spacing=0.2, hold=0.08):
    result = []
    for i in range(count):
        at = start + i * spacing
        result.extend([(at, packet(at, 1)), (at + hold, packet(at + hold, 0))])
    return result


class Harness:
    def __init__(self, source, reads, *, lcd_delay=0, **overrides):
        self.settings = settings_from(source)
        self.settings.short_refresh_enabled = True
        self.settings.upgrade_enabled = True
        self.settings.__dict__.update(overrides)
        self.reads = deque(reads)
        self.now = 100.0
        self.effects = []
        self.messages = []
        self.logs = []
        self.opens = 0
        self.closes = []
        self.read_count = 0
        self.lcd_delay = lcd_delay
        self.ns = {
            "__builtins__": {name: getattr(builtins, name) for name in
                             ("int", "float", "str", "list", "len", "range", "BlockingIOError", "OSError")},
            "Settings": SimpleNamespace,
            "EVENT": EVENT, "EV_KEY": 1, "BTN_COPY": 0x101, "running": True,
            "time": SimpleNamespace(monotonic=lambda: self.now, sleep=self.sleep),
            "os": SimpleNamespace(read=self.read, close=self.close),
            "open_input": self.open_input,
            "log": lambda cfg, text: self.logs.append(text),
            "lcd_action": self.lcd_action,
            "restore_lcd": lambda cfg: self.effects.append(("restore",)),
            "perform_upgrade": lambda cfg: self.effects.append(("upgrade",)),
            "perform_check": lambda cfg, refresh=True: self.effects.append(("refresh",) if refresh else ("result", "check")),
            "run_system_action": lambda cfg, action: self.effects.append((action,)),
            "run_stream": self.run_stream,
            "refresh_status_led": lambda: self.effects.append(("led",)),
            "update_summary": lambda: "mock update summary",
            "show_update_result": lambda cfg, action: self.effects.append(("result", action)),
        }
        tree = ast.parse(source)
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "loop")
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), self.ns)

    def open_input(self, path):
        assert path == "mock://button_input"
        self.opens += 1
        if not self.reads:
            self.ns["running"] = False
        return 700 + self.opens

    def close(self, fd):
        assert fd >= 701
        self.closes.append(fd)

    def read(self, fd, size):
        assert fd >= 701 and size == EVENT.size * 16
        self.read_count += 1
        if self.read_count > 1000:
            raise AssertionError("read loop did not terminate")
        if not self.reads:
            self.ns["running"] = False
            raise BlockingIOError()
        at, data = self.reads.popleft()
        self.now = max(self.now, round(at, 6))
        if isinstance(data, BaseException):
            raise data
        return data

    def sleep(self, seconds):
        assert seconds >= 0
        self.now += seconds

    def lcd_action(self, cfg, action, title, detail, **kwargs):
        self.messages.append((action, title, detail))
        self.now += self.lcd_delay

    def run_stream(self, cfg, command):
        assert cfg is self.settings and command == self.settings.refresh_command
        self.effects.append(("refresh",))
        return 0

    def run(self):
        assert self.ns["loop"](self.settings) == 0
        return [item[0] for item in self.effects if item[0] in ("upgrade", "reboot", "poweroff", "refresh")]


def baseline_probes(source):
    duplicate = [(101 + i * 0.1, packet(101 + i * 0.1, 1)) for i in range(8)]
    duplicate += [(101.8, packet(101.8, 0)), (104, BlockingIOError())]
    duplicate += [(104.2 + i * 0.1, packet(104.2 + i * 0.1, 1)) for i in range(3)]
    reconnect = [(101, packet(101, 1)), (101.1, OSError("mock unplug")), (105, packet(105, 0))]
    queued = clicks(101, 8) + [(105, BlockingIOError())]
    queued += [(108, packet(105.1, 1) + packet(105.2, 0) + packet(105.3, 1)
                + packet(105.4, 0) + packet(105.5, 1) + packet(105.6, 0))]
    outputs = {}
    for name, reads, delay in (("duplicate_down_reboot", duplicate, 0),
                               ("reconnect_hold_refresh", reconnect, 0),
                               ("queued_before_prompt_reboot", queued, 1)):
        h = Harness(source, reads, lcd_delay=delay)
        outputs[name] = h.run()
    return outputs


class CopyButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE.read_text()

    def harness(self, reads, **kwargs):
        return Harness(self.source, reads, **kwargs)

    def test_baseline_reproductions_are_now_safe(self):
        self.assertEqual(baseline_probes(self.source), {
            "duplicate_down_reboot": [], "reconnect_hold_refresh": [],
            "queued_before_prompt_reboot": []})
    def armed_reads(self, count=8):
        initial = clicks(101, count)
        armed_at = initial[-1][0] + 2.1
        return initial + [(armed_at, BlockingIOError())], armed_at

    def test_valid_action_ranges_and_fresh_confirmation(self):
        for count, expected in ((3, None), (4, "upgrade"), (7, "upgrade"),
                                (8, "reboot"), (12, "reboot"), (13, "poweroff"), (15, "poweroff")):
            with self.subTest(count=count):
                reads, at = self.armed_reads(count)
                reads += clicks(at + 0.3, 3)
                h = self.harness(reads)
                self.assertEqual(h.run(), [expected] if expected else [])

    def test_press_only_does_not_arm_or_confirm(self):
        reads, at = self.armed_reads()
        reads += clicks(at + 0.3, 2) + [(at + 0.8, packet(at + 0.8, 1))]
        self.assertEqual(self.harness(reads).run(), [])

    def test_arm_sequence_cannot_confirm_itself(self):
        reads = clicks(101, 16) + [(107, BlockingIOError())]
        h = self.harness(reads)
        self.assertEqual(h.run(), [])
        self.assertIn(("poweroff", "Confirm", "3 clicks"), h.messages)

    def test_duplicate_down_and_repeats_never_authorize(self):
        for value in (1, 2):
            reads, at = self.armed_reads()
            reads += [(at + 0.3 + i * 0.1, packet(at + 0.3 + i * 0.1, value)) for i in range(20)]
            reads += [(at + 2.4, packet(at + 2.4, 0))]
            self.assertNotIn("reboot", self.harness(reads).run())

    def test_orphan_duplicate_release_and_foreign_events(self):
        reads = [(100.5, packet(100.5, 0)), (100.6, packet(100.6, 1, code=42)),
                 (100.7, packet(100.7, 1, event_type=4))]
        initial, at = self.armed_reads(4)
        reads += initial + [(at + 0.1, packet(at + 0.1, 0))] + clicks(at + 0.3, 3)
        self.assertEqual(self.harness(reads).run(), ["upgrade"])

    def test_unknown_value_invalidates_pending(self):
        reads, at = self.armed_reads()
        reads += [(at + 0.3, packet(at + 0.3, 9))] + clicks(at + 0.5, 3)
        self.assertEqual(self.harness(reads).run(), [])

    def test_noise_too_short_or_too_close_is_not_a_click(self):
        for spacing, hold in ((0.02, 0.005), (0.06, 0.04)):
            reads = clicks(101, 20, spacing=spacing, hold=hold) + [(106, BlockingIOError())]
            reads += clicks(106.3, 3)
            self.assertEqual(self.harness(reads).run(), [])

    def test_hold_cancels_pending_instead_of_third_confirmation(self):
        reads, at = self.armed_reads()
        reads += clicks(at + 0.3, 2)
        reads += [(at + 1, packet(at + 1, 1)), (at + 3.1, packet(at + 3.1, 0))]
        reads += clicks(at + 3.5, 3)
        self.assertEqual(self.harness(reads).run(), ["refresh"])

    def test_legacy_hold_is_opt_in_and_clears_pending(self):
        reads, at = self.armed_reads()
        reads += [(at + 0.3, packet(at + 0.3, 1)), (at + 3.4, packet(at + 3.4, 0))]
        reads += clicks(at + 3.7, 3)
        self.assertEqual(self.harness(reads, upgrade_hold_seconds=3).run(), ["upgrade"])
        self.assertEqual(self.harness(reads).run(), ["refresh"])

    def test_short_refresh_option_preserved(self):
        h = self.harness([(101, packet(101, 1)), (103.1, packet(103.1, 0))],
                         short_refresh_enabled=False)
        self.assertEqual(h.run(), [])
        self.assertIn(("result", "check"), h.effects)

    def test_timeout_discards_confirmation_and_old_pending_press(self):
        reads, at = self.armed_reads()
        reads += clicks(at + 0.3, 2)
        reads += [(at + 4.8, packet(at + 4.8, 1)), (at + 5.1, packet(at + 5.1, 0))]
        h = self.harness(reads)
        self.assertEqual(h.run(), [])
        self.assertTrue(any("timed out" in line for line in h.logs))

    def test_reconnect_clears_pending_clicks_and_press(self):
        for disconnect in (OSError("unplug"), b""):
            reads, at = self.armed_reads()
            reads += clicks(at + 0.3, 2)
            reads += [(at + 1, disconnect)] + clicks(at + 2.1, 3)
            h = self.harness(reads)
            self.assertEqual(h.run(), [])
            self.assertEqual(h.opens, 2)
            self.assertEqual(len(h.closes), 2)
        h = self.harness([(101, packet(101, 1)), (101.1, b""), (105, packet(105, 0))])
        self.assertEqual(h.run(), [])

    def test_fragmented_records_reassemble_but_truncation_never_confirms(self):
        reads, at = self.armed_reads(4)
        for captured, data in clicks(at + 0.3, 3):
            reads += [(captured, data[:7]), (captured, data[7:])]
        self.assertEqual(self.harness(reads).run(), ["upgrade"])
        reads, at = self.armed_reads()
        confirm = b"".join(data for _, data in clicks(at + 0.3, 3))
        reads += [(at + 1, confirm[:-1]), (at + 1.1, BlockingIOError()), (at + 1.2, b"")]
        self.assertEqual(self.harness(reads).run(), [])

    def test_syn_dropped_invalidates_and_ignores_until_report(self):
        reads, at = self.armed_reads()
        reads += [(at + 0.2, packet(at + 0.2, 0, event_type=0, code=3))]
        reads += clicks(at + 0.3, 5)
        reads += [(at + 1.5, packet(at + 1.5, 0, event_type=0, code=0))]
        reads += clicks(at + 1.8, 3)
        self.assertEqual(self.harness(reads).run(), [])

    def test_slow_prompt_rejects_queued_confirmation_even_in_same_batch(self):
        reads, at = self.armed_reads()
        stale = b"".join(data for _, data in clicks(at + 0.1, 3))
        reads += [(at + 2, stale)]
        self.assertEqual(self.harness(reads, lcd_delay=1).run(), [])
        reads, at = self.armed_reads()
        reads += clicks(at + 1.3, 1)
        stale = b"".join(data for _, data in clicks(at + 1.5, 2))
        reads += [(at + 3, stale)]
        self.assertEqual(self.harness(reads, lcd_delay=1).run(), [])

    def test_fresh_confirmations_survive_slow_prompt_when_truly_fresh(self):
        reads, at = self.armed_reads()
        reads += clicks(at + 1.3, 3, spacing=1.3)
        self.assertEqual(self.harness(reads, lcd_delay=1).run(), ["reboot"])

    def test_no_arm_between_queued_batches(self):
        a = b"".join(data for _, data in clicks(101, 8))
        b = b"".join(data for _, data in clicks(103, 3))
        h = self.harness([(106, a), (106, b), (107, BlockingIOError())])
        self.assertEqual(h.run(), [])
        self.assertEqual(sum(title == "Confirm" for _, title, _ in h.messages), 1)

    def test_invalid_timestamps_fail_closed(self):
        for bad in (packet(999, 1), EVENT.pack(101, 1_000_001, 1, 0x101, 1), packet(99, 1)):
            reads, at = self.armed_reads()
            reads += [(at + 0.2, bad)] + clicks(at + 0.4, 3)
            self.assertEqual(self.harness(reads).run(), [])

    def test_missing_input_retries_with_no_state(self):
        h = self.harness(clicks(106, 4) + [(109, BlockingIOError())] + clicks(109.3, 3))
        original = h.open_input
        attempts = []
        def sometimes_missing(path):
            attempts.append(path)
            return None if len(attempts) == 1 else original(path)
        h.ns["open_input"] = sometimes_missing
        self.assertEqual(h.run(), ["upgrade"])
        self.assertEqual(len(attempts), 2)

    def test_custom_button_code_uses_same_safe_loop(self):
        reads, at = self.armed_reads(4)
        reads += clicks(at + 0.3, 3)
        mapped = []
        for captured, data in reads:
            if isinstance(data, bytes):
                sec, usec, kind, _, value = EVENT.unpack(data)
                data = EVENT.pack(sec, usec, kind, 0x102, value)
            mapped.append((captured, data))
        self.assertEqual(self.harness(mapped, button_code=0x102).run(), ["upgrade"])

    def test_event_capture_duration_not_processing_delay_controls_hold(self):
        reads = [(101, packet(101, 1)), (106, packet(101.08, 0))]
        h = self.harness(reads)
        self.assertEqual(h.run(), [])
        self.assertFalse(any("check hold" in line for line in h.logs))

    def test_loop_effect_exception_still_closes_input(self):
        reads, at = self.armed_reads(4)
        h = self.harness(reads + clicks(at + 0.3, 3))
        def fail(cfg):
            raise RuntimeError("injected final effect failure")
        h.ns["perform_upgrade"] = fail
        with self.assertRaisesRegex(RuntimeError, "injected"):
            h.run()
        self.assertEqual(len(h.closes), 1)


class OpenInputTests(unittest.TestCase):
    def test_clock_ioctl_success_and_failure_close(self):
        for failure in (None, OSError("clock unsupported")):
            calls = []
            def ioctl(fd, request, value):
                calls.append((fd, request, struct.unpack("i", value)[0]))
                if failure is not None:
                    raise failure
            ns = {"Settings": SimpleNamespace, "os": SimpleNamespace(
                O_RDONLY=0, O_NONBLOCK=2048, open=lambda path, flags: 555,
                close=lambda fd: calls.append(("close", fd))),
                "fcntl": SimpleNamespace(ioctl=ioctl), "struct": struct,
                "time": SimpleNamespace(CLOCK_MONOTONIC=1),
                "__builtins__": {"int": int, "str": str, "OSError": OSError}}
            node = next(n for n in ast.parse(SOURCE.read_text()).body
                        if isinstance(n, ast.FunctionDef) and n.name == "open_input")
            exec(compile(ast.Module(body=[node], type_ignores=[]), "<open-input>", "exec"), ns)
            self.assertEqual(ns["open_input"]("mock://input"), None if failure else 555)
            self.assertIn((555, 0x400445A0, 1), calls)
            self.assertEqual(("close", 555) in calls, failure is not None)


class BackendRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="copy-recovery-", dir=os.environ["TMPDIR"])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.cfg = SimpleNamespace(lock_file=str(self.root / "action.lock"),
                                   running_flag=str(self.root / "running.flag"),
                                   upgrade_enabled=True, short_refresh_enabled=True, refresh_command="MOCK_REFRESH_ONLY",
                                   upgrade_command="MOCK_INSTALL_ONLY")
        self.calls = []
        self.failures = {}
        self.codes = deque()
        def record(name):
            def effect(*args, **kwargs):
                self.calls.append((name, args[1:] if name in ("lcd", "result") else ()))
                if name in self.failures:
                    raise self.failures[name]
                return "mock summary" if name == "summary" else None
            return effect
        def checked(path):
            result = Path(path).resolve()
            assert result.is_relative_to(self.root), "refused non-fixture IO"
            return result
        def safe_open(path, *args, **kwargs):
            return open(checked(path), *args, **kwargs)
        def run(cfg, command):
            assert cfg is self.cfg and command in ("MOCK_REFRESH_ONLY", "MOCK_INSTALL_ONLY")
            self.calls.append((command, ()))
            if "command" in self.failures:
                raise self.failures["command"]
            return self.codes.popleft() if self.codes else 0
        self.ns = {"Settings": SimpleNamespace, "open": safe_open, "fcntl": fcntl,
                   "os": SimpleNamespace(path=SimpleNamespace(dirname=lambda p: str(checked(p).parent)),
                         makedirs=lambda p, **kw: checked(p).mkdir(parents=True, exist_ok=True),
                         getpid=lambda: 777, unlink=lambda p: checked(p).unlink()),
                   "run_stream": run,
                   "__builtins__": {name: getattr(builtins, name) for name in
                         ("bool", "str", "Exception", "FileNotFoundError", "BlockingIOError")}}
        for name, key in (("log", "log"), ("lcd_action", "lcd"), ("restore_lcd", "restore"),
                          ("refresh_status_led", "led"), ("show_update_result", "result"),
                          ("update_summary", "summary")):
            self.ns[name] = record(key)
        names = {"finish_update_action", "perform_upgrade", "perform_install_only", "perform_check"}
        nodes: list[ast.stmt] = [n for n in ast.parse(SOURCE.read_text()).body if isinstance(n, ast.FunctionDef) and n.name in names]
        assert {n.name for n in nodes if isinstance(n, ast.FunctionDef)} == names
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "<backend-only>", "exec"), self.ns)

    def names(self):
        return [name for name, _ in self.calls]

    def assert_recovered(self):
        self.assertFalse(Path(self.cfg.running_flag).exists())
        with open(self.cfg.lock_file, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertIn("led", self.names())
        self.assertIn("restore", self.names())

    def test_all_actions_obey_actual_local_flock_contention(self):
        for name in ("perform_check", "perform_upgrade", "perform_install_only"):
            with self.subTest(action=name):
                self.calls.clear()
                with open(self.cfg.lock_file, "w") as holder:
                    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    Path(self.cfg.running_flag).write_text("other owner")
                    self.ns[name](self.cfg)
                    self.assertEqual(Path(self.cfg.running_flag).read_text(), "other owner")
                    Path(self.cfg.running_flag).unlink()
                self.assertNotIn("MOCK_REFRESH_ONLY", self.names())
                self.assertNotIn("MOCK_INSTALL_ONLY", self.names())
                self.assert_recovered()

    def test_nonzero_install_is_error_not_normal_result(self):
        for name, codes in (("perform_upgrade", [0, 42]), ("perform_install_only", [42])):
            with self.subTest(action=name):
                self.calls.clear()
                self.codes = deque(codes)
                self.ns[name](self.cfg)
                self.assertNotIn("result", self.names())
                self.assertTrue(any(n == "lcd" and args[1] == "Apt error" for n, args in self.calls))
                self.assert_recovered()

    def test_refresh_failure_does_not_install_or_show_success(self):
        for name in ("perform_check", "perform_upgrade"):
            self.calls.clear()
            self.codes = deque([37])
            self.ns[name](self.cfg)
            self.assertNotIn("MOCK_INSTALL_ONLY", self.names())
            self.assertNotIn("result", self.names())
            self.assert_recovered()

    def test_success_calls_normal_result_and_cleans(self):
        for name in ("perform_check", "perform_upgrade", "perform_install_only"):
            self.calls.clear()
            self.ns[name](self.cfg)
            self.assertIn("result", self.names())
            self.assert_recovered()

    def test_logging_presentation_summary_command_exceptions_cleanup(self):
        for action in ("perform_check", "perform_upgrade", "perform_install_only"):
            for effect in ("log", "lcd", "command", "summary", "result"):
                with self.subTest(action=action, effect=effect):
                    self.calls.clear()
                    self.failures = {effect: RuntimeError("primary " + effect)}
                    with self.assertRaisesRegex(RuntimeError, "primary " + effect):
                        self.ns[action](self.cfg)
                    self.assert_recovered()
        self.failures.clear()

    def test_cleanup_failures_do_not_mask_original_or_skip_other_cleanup(self):
        for action in ("perform_check", "perform_upgrade", "perform_install_only"):
            self.calls.clear()
            primary = ValueError("original presentation error")
            self.failures = {"lcd": primary, "led": OSError("mock led error"),
                             "restore": OSError("mock restore error"), "log": RuntimeError("log error")}
            # Fail at LCD, not at the earlier log, to preserve exact original.
            self.ns["log"] = lambda *args: None
            with self.assertRaises(ValueError) as raised:
                self.ns[action](self.cfg)
            self.assertIs(raised.exception, primary)
            self.assert_recovered()

    def test_partial_flag_write_failure_is_cleaned(self):
        original = self.ns["open"]
        class BrokenFlag:
            def __enter__(inner):
                Path(self.cfg.running_flag).touch()
                return inner
            def write(inner, value):
                raise OSError("mock partial flag write")
            def __exit__(inner, *args):
                return False
        def injected(path, *args, **kwargs):
            return BrokenFlag() if path == self.cfg.running_flag else original(path, *args, **kwargs)
        self.ns["open"] = injected
        for action in ("perform_upgrade", "perform_install_only"):
            self.calls.clear()
            with self.assertRaisesRegex(OSError, "partial flag"):
                self.ns[action](self.cfg)
            self.assert_recovered()

    def test_flag_open_failure_after_create_releases_lock(self):
        original = self.ns["open"]
        def fail_flag(path, *args, **kwargs):
            if path == self.cfg.running_flag:
                Path(path).touch()
                raise OSError("mock flag open failure after create")
            return original(path, *args, **kwargs)
        self.ns["open"] = fail_flag
        for action in ("perform_upgrade", "perform_install_only"):
            self.calls.clear()
            with self.assertRaisesRegex(OSError, "flag open"):
                self.ns[action](self.cfg)
            self.assert_recovered()

    def test_unlink_failure_does_not_mask_primary_or_skip_restoration(self):
        original_unlink = self.ns["os"].unlink
        primary = RuntimeError("original command error")
        self.failures["command"] = primary
        def unlink_failure(path):
            raise OSError("mock unlink unavailable")
        self.ns["os"].unlink = unlink_failure
        with self.assertRaises(RuntimeError) as raised:
            self.ns["perform_upgrade"](self.cfg)
        self.assertIs(raised.exception, primary)
        self.assertTrue(Path(self.cfg.running_flag).exists())
        self.assertIn("led", self.names())
        self.assertIn("restore", self.names())
        original_unlink(self.cfg.running_flag)
        self.assert_recovered()

    def test_check_without_refresh_keeps_lock_and_reports_cached_state(self):
        self.ns["perform_check"](self.cfg, refresh=False)
        self.assertNotIn("MOCK_REFRESH_ONLY", self.names())
        self.assertIn("result", self.names())
        self.assert_recovered()


if __name__ == "__main__":
    unittest.main()
