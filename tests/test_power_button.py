"""Power's actual selector loop, AST-isolated with mandatory inert IO/effects.

No daemon import, subprocess, device, PATH substitution or effect fallback.
Capture time and delivery time are independent, including simultaneous sources.
"""
import ast
import builtins
from collections import deque
from pathlib import Path
import struct
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "src/proxnap/power_button.py"
EVENT = struct.Struct("llHHi")


def packet(at, value, event_type=1, code=116):
    sec = int(at)
    return EVENT.pack(sec, round((at - sec) * 1_000_000), event_type, code, value)


def clicks(start, count, *, path="mock://a", spacing=0.2, hold=0):
    return [(round(start + i * spacing, 6), path,
             packet(start + i * spacing, 1) + packet(start + i * spacing + hold, 0))
            for i in range(count)]


class Harness:
    def __init__(self, source, deliveries=(), *, paths=("mock://a",),
                 lcd_delay=0, end=110, aliases=None, fail_register=None,
                 ioctl_error=False, close_error=False, unregister_error=False,
                 selector_error=False, fstat_error=False, selector_close_error=False,
                 open_error=False, **overrides):
        tree = ast.parse(source)
        settings = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Settings")
        values = {n.target.id: ast.literal_eval(n.value) for n in settings.body
                  if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.value is not None}
        values.update(overrides)
        values.update(input_paths=list(paths), paths=lambda: list(paths))
        for name in ("lock_file", "log_file", "lcd_port", "lcd_lock_file"):
            values[name] = "mock://" + name
        self.settings = SimpleNamespace(**values)
        self.deliveries = deque(sorted(((round(t, 6), p, data) for t, p, data in deliveries),
                                       key=lambda x: x[0]))
        self.queues = {p: deque() for p in paths}
        self.now, self.end, self.lcd_delay = 100.0, end, lcd_delay
        self.effects, self.messages, self.logs = [], [], []
        self.opened, self.closed, self.ioctls, self.selectors = {}, [], [], []
        self.fd_sequence = 700
        self.aliases = aliases or {}
        self.fail_register = fail_register
        self.ioctl_error, self.close_error = ioctl_error, close_error
        self.unregister_error, self.selector_error = unregister_error, selector_error
        self.fstat_error, self.selector_close_error = fstat_error, selector_close_error
        self.open_error = open_error
        self.read_count = 0
        safe_names = ("int", "float", "str", "list", "dict", "set", "tuple", "len", "range",
                      "sorted", "enumerate", "any", "all", "min", "max", "Exception",
                      "BlockingIOError", "OSError", "ValueError", "RuntimeError")
        self.ns = {
            "__builtins__": {k: getattr(builtins, k) for k in safe_names},
            "Settings": SimpleNamespace, "EVENT": EVENT, "EV_KEY": 1, "KEY_POWER": 116,
            "running": True, "struct": struct,
            "time": SimpleNamespace(monotonic=lambda: self.now, sleep=self.sleep, CLOCK_MONOTONIC=1),
            "os": SimpleNamespace(open=self.open, read=self.read, close=self.close,
                                  fstat=self.fstat, O_RDONLY=0, O_NONBLOCK=2048),
            "fcntl": SimpleNamespace(ioctl=self.ioctl),
            "selectors": SimpleNamespace(DefaultSelector=self.new_selector, EVENT_READ=1),
            "log": lambda cfg, text: self.logs.append(text),
            "lcd_message": self.lcd,
            "run_action": lambda cfg, action: self.effects.append(action),
        }
        nodes: list[ast.stmt] = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                                and n.name in {"open_input", "register_inputs", "loop"}]
        assert len(nodes) == 3
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), self.ns)

    def advance(self, at):
        self.now = max(self.now, round(at, 6))
        while self.deliveries and self.deliveries[0][0] <= self.now:
            _, path, data = self.deliveries.popleft()
            self.queues[path].append(data)
        if self.now >= self.end:
            self.ns["running"] = False

    def sleep(self, seconds):
        self.advance(self.now + seconds)

    def open(self, path, flags):
        assert path.startswith("mock://") and flags == 2048
        if self.open_error:
            raise OSError("mock open failure")
        self.fd_sequence += 1
        self.opened[self.fd_sequence] = path
        return self.fd_sequence

    def fstat(self, fd):
        if self.fstat_error:
            raise OSError("mock metadata failure")
        path = self.opened[fd]
        identity = self.aliases.get(path, list(self.queues).index(path) + 1)
        return SimpleNamespace(st_dev=1, st_ino=identity, st_rdev=identity)

    def ioctl(self, fd, request, arg):
        assert fd in self.opened and request == 0x400445A0 and arg == struct.pack("i", 1)
        self.ioctls.append(fd)
        if self.ioctl_error:
            raise OSError("mock clock unsupported")

    def close(self, fd):
        assert fd in self.opened
        self.closed.append(fd)
        if self.close_error:
            raise OSError("mock close failure")

    def read(self, fd, size):
        assert fd in self.opened and fd not in self.closed and size == EVENT.size * 16
        self.read_count += 1
        assert self.read_count <= 10000, "unbounded read loop"
        self.advance(self.now)
        queue = self.queues[self.opened[fd]]
        if not queue:
            raise BlockingIOError()
        data = queue.popleft()
        if isinstance(data, BaseException):
            raise data
        return data

    def lcd(self, cfg, title, detail):
        assert cfg is self.settings
        self.messages.append((title, detail, self.now))
        self.advance(self.now + self.lcd_delay)

    def new_selector(self):
        owner = self
        class Selector:
            def __init__(self):
                self.fds, self.closed, self.unregistered = [], False, []
            def register(self, fd, event):
                assert fd in owner.opened and event == 1
                if owner.opened[fd] == owner.fail_register:
                    raise OSError("mock register failure")
                self.fds.append(fd)
            def select(self, timeout):
                if owner.selector_error:
                    raise OSError("mock select error")
                owner.advance(owner.now)
                ready = [fd for fd in self.fds if owner.queues[owner.opened[fd]]]
                if not ready:
                    at = min(owner.now + timeout, owner.end)
                    if owner.deliveries:
                        at = min(at, owner.deliveries[0][0])
                    owner.advance(at)
                    ready = [fd for fd in self.fds if owner.queues[owner.opened[fd]]]
                return [(SimpleNamespace(fd=fd), 1) for fd in ready]
            def unregister(self, fd):
                self.unregistered.append(fd)
                if owner.unregister_error:
                    raise OSError("mock unregister failure")
                self.fds.remove(fd)
            def close(self):
                self.closed = True
                if owner.selector_close_error:
                    raise OSError("mock selector close failure")
        selector = Selector()
        self.selectors.append(selector)
        return selector

    def run(self):
        assert self.ns["loop"](self.settings) == 0
        return self.effects


def action_deliveries(count=5, *, start=101, confirm=103, path="mock://a"):
    return clicks(start, count, path=path) + clicks(confirm, 3, path=path)


def baseline_probes(source):
    orphan = [(101 + i * .2, "mock://a", packet(101 + i * .2, 0)) for i in range(5)]
    orphan += [(103 + i * .2, "mock://a", packet(103 + i * .2, 0)) for i in range(3)]
    stale = clicks(101, 5) + [(103 + i * .2, "mock://a", packet(101.9 + i * .2, 1)
                                              + packet(101.9 + i * .2, 0)) for i in range(3)]
    reconnect = clicks(101, 5) + [(102.8, "mock://a", OSError("mock disconnect"))]
    reconnect += [(104 + i * .2, "mock://a", packet(104 + i * .2, 0)) for i in range(3)]
    duplicate = [(t, p, data[:EVENT.size] + data) for t, p, data in action_deliveries()]
    mixed = []
    for t, _, data in action_deliveries():
        mixed += [(t, "mock://a", data[:EVENT.size]), (t, "mock://b", data[EVENT.size:])]
    return {name: Harness(source, events, end=109).run()
            for name, events in (("orphan_releases", orphan), ("stale_capture", stale),
                                 ("reconnect_pending", reconnect), ("duplicate_down", duplicate))} | {
            "mixed_fd_pairs": Harness(source, mixed, paths=("mock://a", "mock://b"), end=109).run()}


class PowerButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE.read_text()

    def harness(self, events=(), **kwargs):
        return Harness(self.source, events, **kwargs)

    def test_baseline_reproductions_are_safe(self):
        self.assertEqual(baseline_probes(self.source), {
            "orphan_releases": [], "stale_capture": [], "reconnect_pending": [],
            "duplicate_down": [], "mixed_fd_pairs": []})

    def test_default_reboot_zero_duration_pairs(self):
        self.assertEqual(self.harness(action_deliveries()).run(), ["reboot"])

    def test_default_poweroff_zero_duration_pairs(self):
        self.assertEqual(self.harness(action_deliveries(10, confirm=104)).run(), ["poweroff"])

    def test_custom_thresholds_preserved(self):
        h = self.harness(clicks(101, 2) + clicks(102.3, 2), reboot_click_count=2,
                         poweroff_click_count=4, confirm_click_count=2)
        self.assertEqual(h.run(), ["reboot"])

    def test_duplicate_down_invalidates_pending(self):
        events = clicks(101, 5) + clicks(103, 2)
        events += [(103.5, "mock://a", packet(103.5, 1) + packet(103.5, 1) + packet(103.5, 0))]
        self.assertEqual(self.harness(events).run(), [])

    def test_press_only_or_repeat_only_never_counts(self):
        for value in (1, 2):
            with self.subTest(value=value):
                events = [(101 + i * .2, "mock://a", packet(101 + i * .2, value)) for i in range(20)]
                self.assertEqual(self.harness(events).run(), [])

    def test_repeat_within_pair_is_not_extra_click(self):
        events = [(t, p, data[:EVENT.size] + packet(t, 2) + data[EVENT.size:])
                  for t, p, data in action_deliveries()]
        self.assertEqual(self.harness(events).run(), ["reboot"])

    def test_duplicate_releases_are_not_additional_clicks(self):
        events = [(t, p, data + packet(t, 0)) for t, p, data in action_deliveries()]
        self.assertEqual(self.harness(events).run(), ["reboot"])

    def test_debounce_is_between_gestures_not_hold_duration(self):
        h = self.harness(clicks(101, 20, spacing=.05) + clicks(104, 3))
        self.assertEqual(h.run(), [])

    def test_unknown_key_value_cancels_pending(self):
        events = clicks(101, 5) + [(103, "mock://a", packet(103, 9))] + clicks(103.2, 3)
        self.assertEqual(self.harness(events).run(), [])

    def test_unrelated_events_do_not_count(self):
        events = [(101 + i * .2, "mock://a", packet(101 + i * .2, 0, code=42)) for i in range(8)]
        self.assertEqual(self.harness(events).run(), [])

    def test_mixed_sources_cannot_pair(self):
        events = []
        for i in range(8):
            at = 101 + i * .3
            events += [(at, "mock://a", packet(at, 1)), (at + .1, "mock://b", packet(at + .1, 0))]
        self.assertEqual(self.harness(events, paths=("mock://a", "mock://b")).run(), [])

    def test_alternating_sources_cannot_build_sequence(self):
        events = [item for i in range(8) for item in clicks(101 + i * .3, 1,
                  path="mock://a" if i % 2 == 0 else "mock://b")]
        self.assertEqual(self.harness(events, paths=("mock://a", "mock://b")).run(), [])

    def test_simultaneous_sources_mirrored_event_is_ambiguous(self):
        events = action_deliveries() + action_deliveries(path="mock://b")
        self.assertEqual(self.harness(events, paths=("mock://a", "mock://b")).run(), [])

    def test_overlapping_down_on_second_source_cancels(self):
        events = clicks(101, 5) + [(103, "mock://a", packet(103, 1)),
                  (103, "mock://b", packet(103, 1)), (103.1, "mock://a", packet(103.1, 0))]
        events += clicks(103.3, 3)
        self.assertEqual(self.harness(events, paths=("mock://a", "mock://b")).run(), [])

    def test_single_active_source_with_two_registered_inputs(self):
        self.assertEqual(self.harness(action_deliveries(), paths=("mock://a", "mock://b")).run(), ["reboot"])

    def test_second_source_cannot_confirm_first(self):
        events = clicks(101, 5) + clicks(103, 3, path="mock://b")
        self.assertEqual(self.harness(events, paths=("mock://a", "mock://b")).run(), [])

    def test_alias_device_registered_once(self):
        h = self.harness(action_deliveries(), paths=("mock://a", "mock://b"),
                         aliases={"mock://a": 1, "mock://b": 1})
        self.assertEqual(h.run(), ["reboot"])
        self.assertEqual(len(h.selectors[0].unregistered), 1)
        self.assertCountEqual(h.closed, h.opened)

    def test_duplicate_path_registered_once(self):
        h = self.harness(action_deliveries(), paths=("mock://a", "mock://a"))
        self.assertEqual(h.run(), ["reboot"])
        self.assertEqual(len(h.opened), 1)

    def test_all_fd_queues_drained_before_arming(self):
        # Old processing-time state arms before reading b; complete queued clicks
        # must instead be invalidated as ambiguous, never promoted to confirmation.
        events = clicks(101, 5) + [(102.45, "mock://b", b"".join(
            packet(101.9 + i * .2, 1) + packet(101.9 + i * .2, 0) for i in range(3)))]
        self.assertEqual(self.harness(events, paths=("mock://a", "mock://b")).run(), [])

    def test_confirmation_captured_during_initial_prompt_is_rejected(self):
        events = clicks(101, 5) + clicks(102.5, 3)
        self.assertEqual(self.harness(events, lcd_delay=.8).run(), [])

    def test_confirmation_captured_during_progress_prompt_is_rejected(self):
        events = clicks(101, 5) + clicks(103.5, 3)
        self.assertEqual(self.harness(events, lcd_delay=.3).run(), [])

    def test_fresh_confirm_after_each_feedback_is_valid(self):
        events = clicks(101, 5) + clicks(103, 3, spacing=.5)
        self.assertEqual(self.harness(events, lcd_delay=.2).run(), ["reboot"])

    def test_expired_confirmation_never_runs(self):
        self.assertEqual(self.harness(clicks(101, 5) + clicks(107, 3)).run(), [])

    def test_initial_sequence_cannot_confirm_itself(self):
        self.assertEqual(self.harness(clicks(101, 16, spacing=.16)).run(), [])

    def test_eof_clears_pending_and_reconnect_requires_full_sequence(self):
        events = clicks(101, 5) + [(102.8, "mock://a", b"")] + clicks(104, 3)
        h = self.harness(events)
        self.assertEqual(h.run(), [])
        self.assertGreater(len(h.opened), 1)
        self.assertCountEqual(h.closed, h.opened)

    def test_error_clears_press_and_pending(self):
        for data in (b"", OSError("mock disconnect")):
            with self.subTest(data=repr(data)):
                events = clicks(101, 5) + clicks(103, 2) + [(103.5, "mock://a", packet(103.5, 1)),
                          (103.6, "mock://a", data), (105, "mock://a", packet(105, 0))]
                self.assertEqual(self.harness(events).run(), [])

    def test_valid_new_sequence_after_reconnect(self):
        events = [(100.5, "mock://a", OSError("mock disconnect"))] + action_deliveries(start=102, confirm=104)
        h = self.harness(events)
        self.assertEqual(h.run(), ["reboot"])
        self.assertCountEqual(h.closed, h.opened)

    def test_fragment_every_split_boundary(self):
        for split in range(1, EVENT.size * 2):
            with self.subTest(split=split):
                events = []
                for t, p, data in action_deliveries():
                    events += [(t, p, data[:split]), (t + .001, p, data[split:])]
                self.assertEqual(self.harness(events).run(), ["reboot"])

    def test_partial_record_blocks_settle(self):
        h = self.harness(clicks(101, 5) + [(102, "mock://a", packet(102, 1)[:5])])
        self.assertEqual(h.run(), [])
        self.assertFalse(any(title.startswith("Confirm") for title, _, _ in h.messages))

    def test_fragment_cannot_cross_reconnect(self):
        pair = packet(101, 1) + packet(101, 0)
        events = [(101, "mock://a", pair[:8]), (101.1, "mock://a", b""),
                  (103, "mock://a", pair[8:])] + clicks(103.2, 4) + clicks(105.3, 3)
        self.assertEqual(self.harness(events).run(), [])

    def test_syn_dropped_ignores_until_report_and_clears_confirmation(self):
        events = clicks(101, 5) + [(103, "mock://a", packet(103, 0, 0, 3))]
        events += clicks(103.2, 3) + [(104, "mock://a", packet(104, 0, 0, 0))] + clicks(104.2, 3)
        self.assertEqual(self.harness(events).run(), [])

    def test_syn_report_recovers_for_new_full_sequence(self):
        events = [(100.5, "mock://a", packet(100.5, 0, 0, 3)),
                  (100.7, "mock://a", packet(100.7, 0, 0, 0))] + action_deliveries()
        self.assertEqual(self.harness(events).run(), ["reboot"])

    def test_future_backward_and_invalid_usec_fail_closed(self):
        for data in (packet(200, 1), packet(100.1, 1), EVENT.pack(103, 1000000, 1, 116, 1)):
            with self.subTest(data=data.hex()):
                events = clicks(101, 5) + clicks(103, 2) + [(103.5, "mock://a", data)] + clicks(103.7, 1)
                self.assertEqual(self.harness(events).run(), [])

    def test_startup_old_queue_is_not_sequence(self):
        events = [(101 + i * .2, "mock://a", packet(99, 1) + packet(99, 0)) for i in range(8)]
        self.assertEqual(self.harness(events).run(), [])

    def test_clock_ioctl_failure_closes_and_never_registers(self):
        h = self.harness(action_deliveries(), ioctl_error=True)
        self.assertEqual(h.run(), [])
        self.assertCountEqual(h.closed, h.opened)
        self.assertTrue(all(not s.fds for s in h.selectors))

    def test_missing_inputs_do_not_leak_selectors(self):
        h = self.harness(open_error=True, end=102)
        self.assertEqual(h.run(), [])
        self.assertEqual(h.opened, {})
        self.assertTrue(all(s.closed for s in h.selectors))

    def test_fstat_failure_closes_unregistered_fd(self):
        h = self.harness(fstat_error=True, end=102)
        self.assertEqual(h.run(), [])
        self.assertCountEqual(h.closed, h.opened)
        self.assertTrue(all(s.closed for s in h.selectors))

    def test_selector_close_error_occurs_only_after_all_fd_cleanup(self):
        h = self.harness(paths=("mock://a", "mock://b"), selector_close_error=True, end=102)
        with self.assertRaises(OSError):
            h.run()
        self.assertCountEqual(h.closed, h.opened)
        self.assertTrue(all(s.closed for s in h.selectors))

    def test_io_flood_is_bounded_and_cannot_arm(self):
        events = [(101, "mock://a", packet(101, 1) + packet(101, 0)) for _ in range(70)]
        h = self.harness(events, end=104)
        self.assertEqual(h.run(), [])
        self.assertLess(h.read_count, 200)
        self.assertCountEqual(h.closed, h.opened)

    def test_one_missing_clock_device_does_not_disable_valid_other_source(self):
        h = self.harness(action_deliveries(path="mock://b"), paths=("mock://a", "mock://b"))
        original_ioctl = h.ns["fcntl"].ioctl
        def ioctl(fd, request, arg):
            if h.opened[fd] == "mock://a":
                raise OSError("mock unsupported source clock")
            original_ioctl(fd, request, arg)
        h.ns["fcntl"].ioctl = ioctl
        self.assertEqual(h.run(), ["reboot"])
        self.assertCountEqual(h.closed, h.opened)

    def test_registration_failure_closes_current_and_previous_fd(self):
        h = self.harness(paths=("mock://a", "mock://b"), fail_register="mock://b", end=102)
        self.assertEqual(h.run(), [])
        self.assertCountEqual(h.closed, h.opened)
        self.assertTrue(all(s.closed for s in h.selectors))

    def test_alias_close_failure_does_not_double_close_descriptor(self):
        h = self.harness(paths=("mock://a", "mock://b"),
                         aliases={"mock://a": 1, "mock://b": 1}, close_error=True, end=102)
        self.assertEqual(h.run(), [])
        self.assertCountEqual(h.closed, h.opened)
        self.assertTrue(all(s.closed for s in h.selectors))

    def test_select_error_cleans_all_and_retries(self):
        h = self.harness(paths=("mock://a", "mock://b"), selector_error=True, end=102)
        self.assertEqual(h.run(), [])
        self.assertCountEqual(h.closed, h.opened)
        self.assertTrue(all(s.closed for s in h.selectors))

    def test_cleanup_failures_do_not_skip_other_resources(self):
        h = self.harness(paths=("mock://a", "mock://b"), unregister_error=True, close_error=True, end=102)
        self.assertEqual(h.run(), [])
        self.assertCountEqual(h.closed, h.opened)
        self.assertTrue(all(s.closed for s in h.selectors))

    def test_foreign_final_batch_cannot_authorize_before_other_fd(self):
        events = clicks(101, 5) + clicks(103, 3) + clicks(103.4, 1, path="mock://b")
        self.assertEqual(self.harness(events, paths=("mock://a", "mock://b")).run(), [])

    def test_dropped_final_batch_on_other_fd_cancels_before_dispatch(self):
        events = clicks(101, 5) + clicks(103, 3)
        events += [(103.4, "mock://b", packet(103.4, 0, 0, 3))]
        self.assertEqual(self.harness(events, paths=("mock://a", "mock://b")).run(), [])

    def test_unknown_tail_after_third_confirmation_cancels_before_dispatch(self):
        events = clicks(101, 5) + clicks(103, 2)
        events += [(103.4, "mock://a", packet(103.4, 1) + packet(103.4, 0) + packet(103.4, 9))]
        self.assertEqual(self.harness(events).run(), [])

    def test_partial_other_fd_blocks_final_dispatch(self):
        events = clicks(101, 5) + clicks(103, 3) + [(103.4, "mock://b", packet(103.4, 1)[:5])]
        self.assertEqual(self.harness(events, paths=("mock://a", "mock://b")).run(), [])

    def test_same_physical_pair_replayed_with_debounce_zero_is_not_new(self):
        events = [(101 + i * .2, "mock://a", packet(101, 1) + packet(101, 0)) for i in range(5)]
        events += clicks(103, 3)
        self.assertEqual(self.harness(events, debounce_seconds=0).run(), [])

    def test_missing_effect_dependency_fails_without_fallback(self):
        h = self.harness(action_deliveries())
        del h.ns["run_action"]
        with self.assertRaises(NameError):
            h.run()
        self.assertCountEqual(h.closed, h.opened)

    def test_missing_io_dependency_fails_without_fallback(self):
        h = self.harness(action_deliveries())
        del h.ns["os"]
        with self.assertRaises(NameError):
            h.run()
        self.assertTrue(all(s.closed for s in h.selectors))


    def test_action_executor_exact_commands_only_through_inert_callbacks(self):
        node = next(n for n in ast.parse(self.source).body
                    if isinstance(n, ast.FunctionDef) and n.name == "run_action")
        class Lock:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
        for action, expected in ((None, "reboot"), ("reboot", "reboot"),
                                 ("poweroff", "poweroff"), ("shutdown", "poweroff"), ("invalid", None)):
            with self.subTest(action=action):
                called = []
                def inert_open(path, mode, encoding):
                    self.assertEqual((path, mode, encoding), ("mock://lock_file", "w", "utf-8"))
                    return Lock()
                ns = {"__builtins__": {k: getattr(builtins, k) for k in ("BlockingIOError", "str")},
                      "Settings": SimpleNamespace, "open": inert_open,
                      "fcntl": SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=lambda f, flags: None),
                      "subprocess": SimpleNamespace(Popen=lambda argv: called.append(argv)),
                      "log": lambda *args: None, "lcd_message": lambda *args: None}
                exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), ns)
                ns["run_action"](self.harness(reboot_enabled=True, poweroff_enabled=True).settings, action)
                self.assertEqual(called, [["systemctl", expected]] if expected else [])
                if expected:
                    del ns["subprocess"]
                    with self.assertRaises(NameError):
                        ns["run_action"](self.harness(reboot_enabled=True, poweroff_enabled=True).settings, action)


if __name__ == "__main__":
    unittest.main()
