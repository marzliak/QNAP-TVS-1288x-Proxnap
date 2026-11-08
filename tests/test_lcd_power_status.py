"""Power information only: AST isolation, inert FD metadata and real loop/flock.

Never import a production daemon. All input/serial/worker/action IO is mandatory
injected; the only real IO is the existing scratch-confined ownership fixture.
"""
import ast
import builtins
from collections import deque
from contextlib import redirect_stdout
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
import tempfile
import textwrap
from types import SimpleNamespace
from typing import Any, Callable
import unicodedata
import unittest

from test_lcd_ownership import Fixture, StrictNamespace, forbidden, SOURCE


ALIAS_A = "/dev/input/by-path/offline-power-a"
ALIAS_B = "/dev/input/by-path/offline-power-b"
EVENT = struct.Struct("llHHi")
NAMES = {"_PowerStatusInputs", "_PowerStatusPopup", "_power_input_identity",
         "_power_status_pages", "_status_ascii", "_status_causes", "_status_seconds",
         "_status_unit_id", "_status_scroll_timeline",
         "load_settings", "parse_bool", "parse_float", "parse_int", "parse_csv"}


def event(stamp=100.0, value=1, kind=1, code=116):
    sec = int(stamp)
    return EVENT.pack(sec, round((stamp - sec) * 1_000_000), kind, code, value)


def status(**changes):
    result = dict(timestamp=100, check_seconds=30, status="ok", reasons=[], warnings=[], unavailable=[])
    result.update(changes)
    return json.dumps(result)


class PowerIO:
    def __init__(self):
        self.wall = 100.0
        self.aliases = {ALIAS_A: "/dev/input/event101", ALIAS_B: "/dev/input/event102"}
        self.devices = {}
        for index, path in enumerate(self.aliases.values()):
            self.devices[path] = dict(mode=stat.S_IFCHR, rdev=os.makedev(13, 101 + index),
                                      name="Power Button", power=True)
        self.channels = {}
        self.fd_paths = {}
        self.opens = []
        self.closed = []
        self.reads = []
        self.payload = status()
        self.status_reads = 0
        self.fail_open = False
        self.bad_actual = None
        self.bad_dev = False
        self.os = StrictNamespace(open=self.open, close=self.close, read=self.read, fstat=self.fstat,
                                  major=os.major, minor=os.minor, O_RDONLY=os.O_RDONLY,
                                  O_NONBLOCK=os.O_NONBLOCK, O_NOFOLLOW=os.O_NOFOLLOW,
                                  O_CLOEXEC=os.O_CLOEXEC, path=StrictNamespace(
                                      realpath=self.realpath, isabs=os.path.isabs, basename=os.path.basename))

    def realpath(self, path):
        if path.startswith('/proc/self/fd/'):
            fd = int(path.rsplit('/', 1)[1])
            return self.bad_actual or self.fd_paths[fd]
        if path in self.devices:
            return path
        return self.aliases.get(path, path)

    def open(self, path, flags):
        assert path in self.devices, "non-fixture open"
        assert flags == os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW
        self.opens.append((path, flags))
        if self.fail_open:
            raise FileNotFoundError('synthetic missing input')
        fd = 2000 + len(self.opens)
        self.fd_paths[fd] = path
        self.channels[fd] = deque()
        return fd

    def close(self, fd):
        assert fd in self.channels, 'non-fixture close or double-close'
        self.closed.append(fd)
        del self.channels[fd]
        del self.fd_paths[fd]

    def fstat(self, fd):
        assert fd in self.channels
        item = self.devices[self.fd_paths[fd]]
        return SimpleNamespace(st_mode=item['mode'], st_rdev=item['rdev'])

    def read(self, fd, count):
        assert fd in self.channels and count == EVENT.size * 32
        self.reads.append(fd)
        if not self.channels[fd]:
            raise BlockingIOError()
        chunk = self.channels[fd].popleft()
        if isinstance(chunk, BaseException):
            raise chunk
        if len(chunk) > count:
            self.channels[fd].appendleft(chunk[count:])
        return chunk[:count]

    def feed(self, *chunks, alias=ALIAS_A):
        path = self.aliases[alias]
        fd = next(fd for fd, actual in self.fd_paths.items() if actual == path)
        self.channels[fd].extend(chunks)

    def metadata(self, path, default=''):
        for actual, device in self.devices.items():
            base = '/sys/class/input/' + os.path.basename(actual)
            if path == base + '/dev':
                return '0:0' if self.bad_dev else f"{os.major(device['rdev'])}:{os.minor(device['rdev'])}"
            if path == base + '/device/name':
                return device['name']
            if path == base + '/device/capabilities/key':
                if 'bitmap' in device:
                    return device['bitmap']
                bits, wordbits = 1 << 116 if device['power'] else 1, struct.calcsize('L') * 8
                words = []
                while bits:
                    words.insert(0, f'{bits & ((1 << wordbits) - 1):x}')
                    bits >>= wordbits
                return ' '.join(words)
        raise AssertionError('non-fixture metadata read: ' + path)

    def status_open(self, path, mode, **kwargs):
        assert path == 'offline-status' and mode == 'r' and kwargs == {'encoding': 'utf-8'}
        self.status_reads += 1
        if isinstance(self.payload, BaseException):
            raise self.payload
        return io.StringIO(self.payload)

    def namespace(self) -> dict[str, Any]:
        return dict(os=self.os, struct=struct, re=re, stat=stat, math=math, textwrap=textwrap,
                    unicodedata=unicodedata, json=json, open=self.status_open,
                    read_file=self.metadata, time=StrictNamespace(time=lambda: self.wall),
                    LCD_WIDTH=16, STATUS_STATE_FILE='offline-status')


def load_power(io_fixture, namespace=None):
    if io_fixture is None:
        raise ValueError('Mandatory inert IO fixture missing')
    ns = io_fixture.namespace() if namespace is None else namespace
    ns.setdefault('__builtins__', dict(vars(builtins), open=forbidden, __import__=forbidden))
    ns.setdefault('Settings', SimpleNamespace)  # annotation only; settings tests inject real AST Settings
    nodes: list[ast.stmt] = [node for node in ast.parse(SOURCE.read_text()).body
             if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in NAMES]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), ns)
    return ns


class InputTests(unittest.TestCase):
    def setUp(self):
        self.io = PowerIO()
        self.ns = load_power(self.io)
        self.reader = self.ns['_PowerStatusInputs']()
        self.reader.configure((ALIAS_A,), 0.0)
        self.assertFalse(self.reader.poll(0.0, True))
        self.addCleanup(self.reader.close)

    def poll(self, wall=100.1, mono=0.1, accept=True):
        self.io.wall = wall
        return self.reader.poll(mono, accept)

    def test_explicit_containment_and_no_production_effects(self):
        with self.assertRaises(ValueError):
            load_power(None)
        for call in (lambda: self.io.os.write(2001, b'x'),
                     lambda: self.io.os.open('/dev/ttyS1', os.O_RDWR),
                     lambda: self.io.metadata('/sys/class/hwmon/anything'),
                     lambda: self.io.os.ioctl(2001, 0)):
            with self.assertRaises(AssertionError):
                call()

    def test_every_packet_split_and_coalesced_instant_press_release(self):
        for split in range(1, EVENT.size):
            self.io.feed(event(100, 0), event(100.1)[:split], BlockingIOError())
            self.assertFalse(self.poll())
            self.io.feed(event(100.1)[split:], event(100.1, 0))
            self.assertTrue(self.poll())
            self.reader.last_press = -float('inf')
            self.assertLess(len(self.reader.sources[ALIAS_A]['buffer']), EVENT.size)
        self.io.feed(event(100.1) + event(100.1, 0))
        self.reader.last_press = -float('inf')
        self.assertTrue(self.poll())
        self.assertFalse(self.poll())

    def test_duplicate_same_fd_two_sources_repeat_and_release(self):
        self.reader.configure((ALIAS_A, ALIAS_B), 0.1)
        self.poll()
        self.io.feed(event(100.2) + event(100.2, 2) + event(100.2, 1) + event(100.2, 0))
        self.io.feed(event(100.2) + event(100.2, 0), alias=ALIAS_B)
        self.assertTrue(self.poll(100.3))
        self.io.feed(event(100.2) + event(100.2, 0), alias=ALIAS_B)
        self.assertFalse(self.poll(100.3))
        self.io.feed(event(100.7) + event(100.7, 0))
        self.assertTrue(self.poll(100.8))
        self.io.feed(event(100.9, 2) + event(100.9))
        self.assertFalse(self.poll(101.0))

    def test_timestamp_origin_stale_future_before_open_and_invalid_microseconds(self):
        for stamp in (1, 98.9, 100.2, 99.99):
            self.io.feed(event(stamp) + event(stamp, 0))
            self.assertFalse(self.poll(100.1), stamp)
        self.io.feed(EVENT.pack(100, 1_000_000, 1, 116, 1) + event(100.1, 0))
        self.assertFalse(self.poll())
        self.io.feed(event(100.1) + event(100.1, 0))
        self.assertTrue(self.poll())

    def test_unrelated_keys_syn_dropped_and_repeat_need_release(self):
        self.io.feed(event(100.1, code=0x100) + event(100.1, kind=0, code=3) + event(100.1))
        self.assertFalse(self.poll())
        self.io.feed(event(100.1, 0) + event(100.1))
        self.assertFalse(self.poll())  # lost frames cannot re-arm before SYN_REPORT
        self.io.feed(event(100.1, kind=0, code=0) + event(100.1, 0) + event(100.1))
        self.assertTrue(self.poll())

    def test_eof_read_error_reconnect_backoff_and_no_old_partial(self):
        for ending in (b'', OSError('disconnected')):
            self.io.feed(event(100.1)[:5], ending)
            self.assertFalse(self.poll())
            self.assertIsNone(self.reader.sources[ALIAS_A]['fd'])
            before = len(self.io.opens)
            self.assertFalse(self.poll(100.2, 1.0))
            self.assertEqual(len(self.io.opens), before)
            self.assertFalse(self.poll(102.2, 2.2))
            self.assertEqual(len(self.io.opens), before + 1)
            self.io.feed(event(102.2) + event(102.2, 0))
            self.assertTrue(self.poll(102.3, 2.3))
            # Re-arm fixture wall/monotonic for the second synthetic disconnect.
            self.reader.last_press = -float('inf')
        self.reader.close()
        self.assertEqual(len(self.io.closed), len(self.io.opens))

    def test_missing_open_retries_not_each_poll_and_config_dedup(self):
        self.reader.close()
        self.io.fail_open = True
        self.reader.configure((ALIAS_A, ALIAS_A, 'relative', ''), 0.0)
        self.assertEqual(tuple(self.reader.sources), (ALIAS_A,))
        self.poll(mono=0.0)
        count = len(self.io.opens)
        for mono in (0.1, 0.2, 1.9):
            self.poll(mono=mono)
        self.assertEqual(len(self.io.opens), count)
        self.poll(mono=2.0)
        self.assertEqual(len(self.io.opens), count + 1)
        self.io.fail_open = False
        self.poll(mono=4.0)
        count = len(self.io.opens)
        self.reader.configure((ALIAS_A, ALIAS_A), 4.0)
        self.poll(mono=4.1)
        self.assertEqual(len(self.io.opens), count)
        self.reader.configure((), 4.2)
        self.assertFalse(self.io.channels)

    def test_aliases_to_same_device_have_only_one_live_fd(self):
        self.io.aliases[ALIAS_B] = self.io.aliases[ALIAS_A]
        self.reader.configure((ALIAS_A, ALIAS_B), 0.1)
        self.poll()
        self.assertEqual(len(self.io.channels), 1)
        self.assertEqual(len(self.io.closed), 1)

    def test_reject_invalid_fd_metadata_even_when_alias_looks_valid(self):
        self.reader.close()
        device = self.io.devices[self.io.aliases[ALIAS_A]]
        for change in ({'mode': stat.S_IFREG}, {'power': False}, {'name': 'qnap8528'}, {'name': 'Keyboard'}):
            saved = device.copy()
            device.update(change)
            self.reader.configure((ALIAS_A,), 0)
            self.assertFalse(self.poll())
            self.assertFalse(self.io.channels)
            self.reader.close()
            device.update(saved)
        for bitmap in ('', '-1', 'not-hex', 'f' * 100, '0 ' * 65):
            device['bitmap'] = bitmap
            self.reader.configure((ALIAS_A,), 0)
            self.assertFalse(self.poll())
            self.assertFalse(self.io.channels)
            self.reader.close()
        device.pop('bitmap')
        for attr, bad in (('bad_actual', '/dev/input/event999'), ('bad_dev', True)):
            setattr(self.io, attr, bad)
            self.reader.configure((ALIAS_A,), 0)
            self.assertFalse(self.poll())
            self.assertFalse(self.io.channels)
            self.reader.close()
            setattr(self.io, attr, None if attr == 'bad_actual' else False)

    def test_external_suspension_discards_fragment_and_overbudget_backlog(self):
        self.io.feed(event(100.1)[:8], BlockingIOError())
        self.assertFalse(self.poll(100.1, accept=False))
        self.io.feed(event(100.1)[8:] + event(100.1, 0))
        self.assertFalse(self.poll(100.2))
        # More than the per-poll budget: timestamp watermark prevents later replay.
        self.io.feed((event(100.3) + event(100.3, 0)) * 100)
        self.assertFalse(self.poll(100.4, accept=False))
        self.assertFalse(self.poll(100.5))
        self.io.feed(event(100.9) + event(100.9, 0))
        self.assertTrue(self.poll(101.0))

    def test_continuous_noise_has_bounded_reads(self):
        self.io.feed(event(100.0, kind=0, code=0) * 1000)
        before = len(self.io.reads)
        self.assertFalse(self.poll())
        self.assertEqual(len(self.io.reads) - before, 4)
        self.assertLess(len(self.reader.sources[ALIAS_A]['buffer']), EVENT.size)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.io = PowerIO()
        self.ns = load_power(self.io)

    def pages(self, payload):
        self.io.payload = payload
        return self.ns['_power_status_pages']()

    def test_coherent_ok_warning_critical_and_critical_over_unavailable(self):
        self.assertEqual(self.pages(status()), [('Estado atual', 'Tudo OK')])
        self.assertEqual(self.pages(status(status='warning', warnings=['backup atrasado'])), [('Aviso 1/1', 'backup atrasado')])
        self.assertEqual(self.pages(status(status='critical', reasons=['pool degradado'], unavailable=['sensor'])),
                         [('Critico 1/1', 'pool degradado')])
        self.assertEqual(self.pages(status(status='critical', reasons=['primeiro', 'segundo'], warnings=['not this'])),
                         [('Critico 1/2', 'primeiro'), ('Critico 2/2', 'segundo')])

    def test_unknown_missing_invalid_stale_future_nonfinite_and_incoherent(self):
        payloads = [FileNotFoundError(), '{', '[]', 'null', status(timestamp=10 ** 400),
                    status(timestamp=69), status(timestamp=101), status(timestamp=True), status(timestamp=100.0),
                    status(check_seconds=0), status(check_seconds=float('nan')), status(check_seconds=float('inf')),
                    status(check_seconds=True), status(status='critical'), status(status='warning'),
                    status(status='ok', reasons=['problem']), status(status='ok', warnings=['problem']),
                    status(status='warning', reasons=['problem'], warnings=['warn']),
                    status(unavailable=['sensor']), status(status='other'), status(warnings=None),
                    status(reasons=[1]), status(status='critical', reasons=['  ']),
                    status(status='warning', warnings=['warn'], unavailable=['sensor']),
                    '{"timestamp":' + '9' * 5000 + '}', '[' * 10000]
        for payload in payloads:
            with self.subTest(payload=str(payload)[:80]):
                self.assertEqual(self.pages(payload), [('Sem dados', 'Desconhecido')])

    def test_check_seconds_default_and_boundary_match_existing_critical_page(self):
        self.io.wall = 130
        payload = json.loads(status())
        del payload['check_seconds']
        self.assertEqual(self.pages(json.dumps(payload)), [('Estado atual', 'Tudo OK')])
        self.io.wall = 130.01
        self.assertEqual(self.pages(json.dumps(payload)), [('Sem dados', 'Desconhecido')])
        self.io.wall = 101
        self.assertEqual(self.pages(status(check_seconds=0.5)), [('Sem dados', 'Desconhecido')])

    def test_long_reasons_are_complete_ascii_readable_and_abuse_is_bounded(self):
        reasons = ['pool archive degraded: device replacement needed', 'temperature sensor missing on CPU zone']
        pages = self.pages(status(status='critical', reasons=reasons))
        self.assertEqual(' '.join(line for _, line in pages), ' '.join(reasons))
        for title, line in pages:
            self.assertTrue(title.startswith('Critico'))
            self.assertLessEqual(len(title), 16)
            self.assertLessEqual(len(line), 16)
            self.assertTrue((title + line).isascii())
        self.assertEqual(self.pages(status(status='critical', reasons=['ação\x00requerida'])), [('Critico 1/1', 'acao requerida')])
        pages = self.pages(status(status='critical', reasons=['x' * 10000]))
        self.assertEqual(len(pages), 32)
        self.assertEqual(pages[-1], ('Critico', 'Mais: limite LCD'))
        self.assertEqual(self.pages('x' * 65537), [('Sem dados', 'Desconhecido')])

    def test_settings_default_optin_paths_finite_seconds(self):
        # Settings is loaded from the pre-existing inert AST ownership harness.
        with tempfile.TemporaryDirectory(prefix='power-settings-', dir=os.environ['TMPDIR']) as root:
            f = Fixture(root)
            try:
                ns = f.load()
                self.ns['Settings'] = ns['Settings']
                for seconds, expected in [('nan', 3), ('inf', 3), ('0.5', 3), ('11', 3), ('bad', 3), ('1', 1), ('10', 10), ('4.5', 4.5)]:
                    self.ns['read_config'] = lambda _path, value=seconds: dict(power_status_enabled='true', power_status_page_seconds=value,
                                                                            power_status_inputs=f'{ALIAS_A},relative,{ALIAS_A},{ALIAS_B}')
                    settings = self.ns['load_settings']('offline-config')
                    self.assertTrue(settings.power_status_enabled)
                    self.assertEqual(settings.power_status_inputs, (ALIAS_A, ALIAS_B))
                    self.assertEqual(settings.power_status_page_seconds, expected)
                self.ns['read_config'] = lambda _path: {}
                settings = self.ns['load_settings']('offline-config')
                self.assertFalse(settings.power_status_enabled)
                self.assertEqual(settings.power_status_inputs, ())
                self.assertEqual(settings.power_status_page_seconds, 3.0)
            finally:
                f.cleanup()


class PowerLoopFixture(Fixture):
    def __init__(self, root, times):
        super().__init__(root, times)
        self.power = PowerIO()
        self.power_events = {}
        self.popups = 0
        self.configure_hook: Callable[[int, Any], None] | None = None

    def load(self):
        ns = super().load()
        injected = self.power.namespace()
        real_lock_open = ns['open']
        serial_os = ns['os']
        ns.update({key: value for key, value in injected.items() if key not in {'os', 'open', 'time'}})
        ns['os'] = StrictNamespace(
            **{**vars(serial_os), **vars(self.power.os),
               'open': lambda path, flags: self.serial_open(path, flags) if path == 'offline-panel' else self.power.open(path, flags),
               'read': lambda fd, size: self.serial_read(fd, size) if fd == 901 else self.power.read(fd, size),
               'close': lambda fd: self.serial_close(fd) if fd == 901 else self.power.close(fd),
               'write': self.write})
        ns['open'] = lambda path, mode, **kwargs: self.power.status_open(path, mode, **kwargs) if path == 'offline-status' else real_lock_open(path, mode, **kwargs)
        ns['time'].time = lambda: 1000 + self.times[min(self.tick, len(self.times) - 1)]
        old_load = ns['load_settings']
        load_power(self.power, ns)
        ns['load_settings'] = old_load
        ns['current_fan_mode'] = lambda: 'auto'  # inert display-only value
        reader_class = ns['_PowerStatusInputs']
        fixture = self

        class Reader(reader_class):
            def poll(self, mono, accept):
                # Materialize only queued synthetic packets on already-open fake FDs.
                fixture.power.wall = ns['time'].time()
                for alias, chunks in fixture.power_events.pop(fixture.tick, {}).items():
                    fixture.power.feed(*chunks, alias=alias)
                return super().poll(mono, accept)

        popup_class = ns['_PowerStatusPopup']

        class Popup(popup_class):
            def __init__(self, *args):
                fixture.popups += 1
                super().__init__(*args)

        ns['_PowerStatusInputs'], ns['_PowerStatusPopup'] = Reader, Popup
        return ns

    def load_settings(self, config):
        settings = super().load_settings(config)
        settings.power_status_enabled = True
        settings.power_status_inputs = (ALIAS_A, ALIAS_B)
        settings.power_status_page_seconds = 1.0
        if self.configure_hook:
            self.configure_hook(self.tick, settings)
        self.power.payload = status(timestamp=int(1000 + self.times[self.tick]), status='critical', reasons=['pool degraded'])
        return settings

    def power_click(self, tick, alias=ALIAS_A):
        stamp = 1000 + self.times[tick]
        self.power_events.setdefault(tick, {})[alias] = [event(stamp) + event(stamp, 0)]


class RunTests(unittest.TestCase):
    def setUp(self):
        scratch = os.environ.get('TMPDIR')
        self.assertTrue(scratch and Path(scratch).is_dir(), 'explicit runtime TMPDIR required')
        self.temp = tempfile.TemporaryDirectory(prefix='lcd-power-loop-', dir=scratch)
        self.addCleanup(self.temp.cleanup)

    def fixture(self, times):
        f = PowerLoopFixture(self.temp.name, times)
        self.addCleanup(f.cleanup)
        f.load()
        return f

    def run_fixture(self, f):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(f.run(), 0)
        self.assertFalse(f.power.channels)
        self.assertEqual(len(f.power.closed), len(f.power.opens))
        self.assertEqual(f.actions, [])
        self.assertTrue(f.thread_joined)
        f.assert_available()
        self.assertEqual(f.tick, len(f.times))  # every iteration reached main sleep

    def test_real_run_press_popup_safe_return_and_no_actions_from_admin_modes(self):
        for mode, action in [('confirm', 'Reboot'), ('confirm', 'Shutdown'), ('confirm', 'UpdateRefreshRun'), ('updates', None), ('fan', None)]:
            with self.subTest(mode=mode, action=action):
                f = self.fixture([10, 10.2, 10.4, 11.2, 12, 14, 15])
                f.ui_hook = lambda ui: (setattr(ui, 'mode', mode), setattr(ui, 'confirm_action', action),
                                        setattr(ui, 'press_mask', 1), setattr(ui, 'press_started', 10))
                f.power_click(1)
                f.power_click(1, ALIAS_B)
                f.events = {1: [1], 2: [2, 0, 1], 3: [1], 4: [1], 5: [0]}
                self.run_fixture(f)
                self.assertEqual(f.popups, 1)
                self.assertEqual(f.ui.mode, 'home')
                self.assertIsNone(f.ui.confirm_action)
                self.assertEqual(f.ui.press_mask, 0)
                self.assertIn((1, b'\x4d\x0c\x00\x10Critico 1/1     '), f.writes)
                self.assertIn((1, b'\x4d\x0c\x01\x10pool degraded   '), f.writes)

    def test_power_wakes_offscreen_and_home_lock_survives(self):
        f = self.fixture([10, 400, 401, 401.5, 402.1])
        f.ui_hook = lambda ui: (setattr(ui, 'home_frame_lock', 2), setattr(ui, 'home_visible_frame', 2))
        f.power_click(2)
        self.run_fixture(f)
        self.assertIn((1, b'\x4d\x5e\x00'), f.writes)
        self.assertIn((2, b'\x4d\x5e\x01'), f.writes)
        self.assertEqual(f.ui.home_frame_lock, 2)
        self.assertEqual(f.ui.mode, 'home')
        self.assertEqual(f.popups, 1)

    def test_navigation_snapshot_resumes_manual_timer_without_hidden_rotation(self):
        f = self.fixture([10, 10.2, 10.5, 11.3, 11.5])
        f.ui_hook = lambda ui: (setattr(ui, 'mode', 'section'), setattr(ui, 'group_index', 0),
                                setattr(ui, 'manual_until', 11), setattr(ui, 'home_frame_lock', 1))
        f.power_click(1)
        f.events = {2: [2, 0, 1, 0]}
        self.run_fixture(f)
        self.assertEqual(f.ui.mode, 'section')
        self.assertEqual(f.ui.group_index, 0)
        self.assertEqual(f.ui.page_by_group['Status'], 0)
        self.assertAlmostEqual(f.ui.manual_until, 12.1)
        self.assertEqual(f.ui.home_frame_lock, 1)

    def test_external_owner_discards_requests_does_not_replay_or_write(self):
        f = self.fixture([10, 10.2, 10.4, 10.6, 10.8, 11, 12])
        f.hooks = {0: f.hold_external, 2: f.release_external}
        f.power_click(1)
        f.power_click(2, ALIAS_B)
        f.power_click(4)
        self.run_fixture(f)
        self.assertFalse(any(tick in {1, 2} for tick, _ in f.writes))
        self.assertEqual(f.popups, 1)
        popup_ticks = [tick for tick, data in f.writes if b'Critico' in data]
        self.assertTrue(popup_ticks and min(popup_ticks) == 4)

    def test_external_owner_interrupts_popup_and_does_not_resume_it(self):
        f = self.fixture([10, 10.2, 10.4, 10.6, 10.8])
        f.power_click(1)
        f.hooks = {1: f.hold_external, 2: f.release_external}
        self.run_fixture(f)
        self.assertEqual(f.popups, 1)
        self.assertFalse(any(tick >= 2 and b'Critico' in data for tick, data in f.writes))

    def test_disabled_default_no_input_and_reload_does_not_duplicate_fds(self):
        f = self.fixture([10, 16, 22, 28, 34])
        f.configure_hook = lambda tick, settings: setattr(settings, 'power_status_enabled', tick in {1, 2})
        self.run_fixture(f)
        self.assertEqual(f.popups, 0)
        self.assertEqual(len(f.power.opens), 2)
        f = self.fixture([10, 16, 22])
        f.configure_hook = lambda _tick, settings: setattr(settings, 'enabled', False)
        self.run_fixture(f)
        self.assertEqual(f.power.opens, [])

    def test_reload_removes_inputs_closes_and_reenable_starts_fresh(self):
        f = self.fixture([10, 16, 22, 28])
        f.configure_hook = lambda tick, settings: setattr(settings, 'power_status_inputs', (ALIAS_A,) if tick == 1 else (ALIAS_A, ALIAS_B))
        self.run_fixture(f)
        self.assertEqual(len(f.power.opens), 3)
        self.assertEqual(f.popups, 0)

    def test_multipage_popup_exhausts_snapshot_without_hidden_ui_or_extension(self):
        f = self.fixture([10, 10.2, 10.6, 11.2, 11.6, 12.3, 12.5, 13.3, 13.5])
        f.power.payload = status(timestamp=1010, status='critical', reasons=['first reason', 'second reason', 'third reason'])
        original_settings = f.load_settings

        def fixed_snapshot(config):
            settings = original_settings(config)
            f.power.payload = status(timestamp=1010, status='critical', reasons=['first reason', 'second reason', 'third reason'])
            return settings

        f.ns['load_settings'] = fixed_snapshot

        def guard_ui(ui):
            ui.home_frame_lock = 2
            for name in ('tick', 'process_mask'):
                original = getattr(ui, name)

                def guarded(*args, method=original):
                    self.assertNotIn(f.tick, {1, 2, 3, 4, 5, 6}, 'hidden UI processing during popup')
                    return method(*args)

                setattr(ui, name, guarded)

        f.ui_hook = guard_ui
        f.power_click(1)
        f.power_click(4)  # fresh but already displaying: consume, do not extend
        f.events = {2: [1, 0, 2, 0], 5: [1], 7: [1], 8: [0]}
        self.run_fixture(f)
        self.assertEqual(f.popups, 1)
        self.assertEqual(f.power.status_reads, 1)
        for tick, text in [(1, b'first reason'), (3, b'second reason'), (5, b'third reason')]:
            self.assertTrue(any(t == tick and text in data for t, data in f.writes))
        self.assertTrue(any(t == 7 and b'HOME' in data for t, data in f.writes))
        self.assertEqual(f.ui.home_frame_lock, 2)
        self.assertEqual(f.ui.press_mask, 0)

    def test_fresh_navigation_after_popup_is_not_replayed_or_lost(self):
        f = self.fixture([10, 10.2, 10.4, 11.3, 11.5, 11.8])
        f.power_click(1)
        f.events = {2: [1, 0], 4: [1], 5: [0]}
        self.run_fixture(f)
        self.assertEqual(f.ui.mode, 'cover')
        self.assertEqual(f.ui.group_index, 0)
        self.assertEqual(f.popups, 1)

    def test_reload_disables_popup_closes_inputs_and_reenable_does_not_replay(self):
        f = self.fixture([10, 11, 16, 22, 28])
        f.configure_hook = lambda tick, settings: setattr(settings, 'power_status_enabled', tick != 2)
        f.power_click(1)
        self.run_fixture(f)
        self.assertEqual(f.popups, 1)
        self.assertEqual(len(f.power.opens), 4)
        self.assertFalse(any(tick >= 2 and b'Critico' in data for tick, data in f.writes))

    def test_popup_run_error_closes_every_fake_fd(self):
        f = self.fixture([10, 10.2])
        f.power_click(1)
        f.write_hook = lambda _data: (_ for _ in ()).throw(OSError('synthetic popup write')) if f.tick == 1 else None
        with self.assertRaisesRegex(OSError, 'popup write'):
            f.run()
        self.assertFalse(f.power.channels)
        self.assertEqual(f.closed, [901])
        self.assertTrue(f.input_closed)
        self.assertTrue(f.thread_joined)
        f.assert_available()


if __name__ == '__main__':
    unittest.main()
