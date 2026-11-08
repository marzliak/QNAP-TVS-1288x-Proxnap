"""Offline ownership regressions: AST only, mandatory IO mocks, real tmp flock.

No daemon import, hardware, subprocess, worker target or production path is used.
Only independent descriptors under the explicitly supplied TMPDIR reach flock.
"""
import ast
import builtins
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import io
import os
from pathlib import Path
import tempfile
import termios
from types import SimpleNamespace
from typing import Any, Callable
import unittest
from contextlib import redirect_stdout

SOURCE = Path(__file__).resolve().parent.parent / 'src/proxnap/lcd.py'


def forbidden(*_args, **_kwargs):
    raise AssertionError('Unexpected IO/subprocess/worker target')


class StrictNamespace(SimpleNamespace):
    def __getattr__(self, name):
        raise AssertionError(f'Unexpected dependency: {name}')


def load_definitions(fixture, source_text=None):
    if fixture is None:
        raise ValueError('Mandatory harmless IO fixture missing')
    tree = ast.parse(SOURCE.read_text() if source_text is None else source_text)
    names = {'Settings', 'LcdPage', 'Lcd', 'LcdUi', 'lcd_transaction',
             'external_message_active', 'fit', 'run', 'lcd_off', 'stop',
             'button_name', 'add_section_prefix'}
    nodes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
            nodes.append(node)
        elif isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) and t.id.isupper() for t in node.targets):
            # Only literal constants; never imports or module initializers.
            ast.literal_eval(node.value)
            nodes.append(node)
    ns = fixture.namespace()
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), ns)
    fixture.ns = ns
    return ns


class LockFile:
    def __init__(self, fixture, raw):
        self.fixture, self.raw = fixture, raw

    def fileno(self):
        return self.raw.fileno()

    def close(self):
        if self.fixture.owner is self:
            self.fixture.owner = None
        self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class Fixture:
    def __init__(self, root, times=(10.0,)):
        self.root = Path(root)
        self.path = str(self.root / 'message.lock')
        self.times = list(times)
        self.tick = 0
        self.ns: dict[str, Any] = {}
        self.owner = None
        self.files = []
        self.writes = []
        self.write_owners = []
        self.delays = []
        self.reads = []
        self.closed = []
        self.events = {}
        self.serial_events = {}
        self.hooks = {}
        self.settings_hook: Callable[..., Any] | None = None
        self.ui_hook: Callable[..., Any] | None = None
        self.write_hook: Callable[..., Any] | None = None
        self.fail_write: int | None = None
        self.fail_open = False
        self.fail_input_open = False
        self.fail_attrs = False
        self.fail_thread_start = False
        self.fail_input_close = False
        self.fail_flock = False
        self.fail_lock_open = False
        self.opened = 0
        self.input_closed = False
        self.thread_started = False
        self.thread_joined = False
        self.actions = []
        self.external = None
        self.lock_attempts = []
        self.settings: Any = None
        self.ui: Any = None

    def hold_external(self):
        if self.external is not None:
            raise AssertionError('Already externally locked')
        self.external = open(self.path, 'a')
        fcntl.flock(self.external, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release_external(self):
        if self.external is not None:
            self.external.close()
            self.external = None

    def cleanup(self):
        self.release_external()
        for file in self.files:
            file.close()

    def lock_open(self, path, mode, **kwargs):
        # Fail on any attempted production path, including accidental fallback.
        candidate = Path(path)
        if not path or not candidate.resolve().is_relative_to(self.root.resolve()):
            raise AssertionError(f'Non-fixture lock path: {path!r}')
        if mode not in {'a', 'w'} or kwargs != {'encoding': 'utf-8'}:
            raise AssertionError('Unexpected lock-open contract')
        if self.fail_lock_open:
            raise PermissionError('synthetic permission denial')
        file = LockFile(self, open(path, mode, **kwargs))
        self.files.append(file)
        return file

    def flock(self, file, flags):
        if file not in self.files or file.raw.closed:
            raise AssertionError('Non-fixture lock descriptor')
        self.lock_attempts.append(flags)
        if self.fail_flock:
            raise OSError('synthetic lock error')
        fcntl.flock(file.fileno(), flags)
        self.owner = None if flags == fcntl.LOCK_UN else file

    def assert_owned(self):
        if self.owner is None or self.owner.raw.closed:
            raise AssertionError('LCD effect without daemon ownership')
        with open(self.path, 'a') as contender:
            try:
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
        raise AssertionError('Independent descriptor acquired during transaction')

    def assert_available(self):
        if self.owner is not None:
            raise AssertionError('Leaked daemon ownership')
        with open(self.path, 'a') as contender:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def write(self, fd, data):
        if fd != 901:
            raise AssertionError('Unexpected output descriptor')
        self.assert_owned()
        self.writes.append((self.tick, data))
        self.write_owners.append(self.owner)
        if self.write_hook:
            self.write_hook(data)
        if self.fail_write == len(self.writes):
            raise OSError('synthetic UART failure')
        return len(data)

    def serial_open(self, port, flags):
        if port != 'offline-panel' or flags != os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK:
            raise AssertionError('Unexpected serial open')
        self.opened += 1
        if self.fail_open:
            raise OSError('synthetic serial-open failure')
        return 901

    def serial_close(self, fd):
        if fd != 901:
            raise AssertionError('Unexpected serial close')
        self.closed.append(fd)

    def attrs(self, fd):
        if fd != 901:
            raise AssertionError('Unexpected termios fd')
        if self.fail_attrs:
            raise OSError('synthetic termios failure')
        return [0, 0, 0, 0, 0, 0, [0] * 32]

    def set_attrs(self, fd, when, attrs):
        if fd != 901 or when != termios.TCSANOW or attrs[4:6] != [termios.B1200] * 2:
            raise AssertionError('UART setup changed')

    def select(self, reads, writes, errors, timeout):
        if reads != [901] or writes or errors or timeout != 0:
            raise AssertionError('Unexpected select')
        self.reads.append(('serial', self.tick))
        return ([901] if self.serial_events.get(self.tick) else [], [], [])

    def serial_read(self, fd, count):
        if fd != 901 or count != 64:
            raise AssertionError('Unexpected serial read')
        return b''.join(b'\x53\x05\x00' + bytes([mask]) for mask in self.serial_events.pop(self.tick, []))

    def sleep(self, seconds):
        if self.owner is not None:
            self.assert_owned()
            if seconds not in {0.05, 0.12, 0.18}:
                raise AssertionError('Non-UART wait while holding ownership')
            self.delays.append(seconds)
            return
        if seconds not in {0.2, 1.0}:
            raise AssertionError('Unexpected loop wait')
        if self.tick in self.hooks:
            self.hooks[self.tick]()
        self.tick += 1
        if self.tick >= len(self.times):
            self.ns['running'] = False

    def load_settings(self, config):
        if config != 'offline-config':
            raise AssertionError('Unexpected config path')
        if self.settings is None:
            self.settings = self.ns['Settings'](lcd_message_lock=self.path, button_input='offline-buttons', config_reload_seconds=5)
        if self.settings_hook:
            self.settings_hook(self.tick, self.settings)
        return self.settings

    def start_job(self, unit, args):
        self.assert_owned()
        self.actions.append((self.tick, unit, args))
        return True

    def namespace(self):
        fixture = self

        class Inputs:
            def __init__(self, path):
                if path != 'offline-buttons':
                    raise AssertionError('Unexpected input path')

            def open(self):
                if fixture.fail_input_open:
                    raise OSError('synthetic input-open failure')

            def read_buttons(self):
                fixture.reads.append(('input', fixture.tick))
                return fixture.events.get(fixture.tick, [])[:]

            def close(self):
                fixture.input_closed = True
                if fixture.fail_input_close:
                    raise OSError('synthetic input-close failure')

        class Cache:
            def get(self):
                return [fixture.ns['LcdPage']('Actions', 'Reboot', 'Reboot', 'OK', 'Reboot')]

            def get_snapshot(self):
                return None

        class Thread:
            def __init__(self, *, target, args, daemon):
                if target is not forbidden or not daemon or args[0] != 'offline-config':
                    raise AssertionError('Unexpected worker')

            def start(self):
                if fixture.fail_thread_start:
                    raise RuntimeError('synthetic thread-start failure')
                fixture.thread_started = True

            def join(self, *, timeout):
                if timeout != 1.0:
                    raise AssertionError('Unbounded worker join')
                fixture.thread_joined = True

        safe_builtins = dict(vars(builtins), open=forbidden, __import__=forbidden)
        return {'__builtins__': safe_builtins, '__name__': 'offline_lcd_ast',
                'contextmanager': contextmanager, 'dataclass': dataclass,
                'open': self.lock_open, 'fcntl': StrictNamespace(flock=self.flock, LOCK_EX=fcntl.LOCK_EX,
                                                              LOCK_NB=fcntl.LOCK_NB, LOCK_UN=fcntl.LOCK_UN),
                'os': StrictNamespace(open=self.serial_open, close=self.serial_close, write=self.write,
                                      read=self.serial_read, O_RDWR=os.O_RDWR, O_NOCTTY=os.O_NOCTTY,
                                      O_NONBLOCK=os.O_NONBLOCK),
                'termios': StrictNamespace(**{name: getattr(termios, name) for name in
                                            ('CS8', 'CREAD', 'CLOCAL', 'B1200', 'VMIN', 'VTIME', 'TCSANOW')},
                                          tcgetattr=self.attrs, tcsetattr=self.set_attrs),
                'select': StrictNamespace(select=self.select),
                'time': StrictNamespace(monotonic=lambda: self.times[min(self.tick, len(self.times) - 1)], sleep=self.sleep),
                'threading': StrictNamespace(Thread=Thread), 'subprocess': StrictNamespace(run=forbidden),
                'load_settings': self.load_settings, 'InputButtons': Inputs, 'PageCache': Cache,
                'page_worker': forbidden, 'start_systemd_job': self.start_job,
                'set_fan_mode': forbidden, 'current_fan_mode': forbidden, 'next_fan_mode': forbidden,
                'home_lines': lambda *_args: ('HOME', 'SCREEN', 0),
                'home_frame_index': lambda *_args: 0, 'running': True}

    def load(self, source_text=None):
        ns = load_definitions(self, source_text)
        fixture = self
        class ConfiguredUi(ns['LcdUi']):
            def __init__(self):
                super().__init__()
                if fixture.ui_hook:
                    fixture.ui_hook(self)
                fixture.ui = self
        ns['LcdUi'] = ConfiguredUi
        return ns

    def run(self, *, leave_off=False):
        args = SimpleNamespace(lcd_port='offline-panel', config='offline-config', leave_lcd_off=leave_off)
        with redirect_stdout(io.StringIO()):
            return self.ns['run'](args)

    def off(self):
        args = SimpleNamespace(lcd_port='offline-panel', config='offline-config')
        return self.ns['lcd_off'](args)


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        scratch = os.environ.get('TMPDIR')
        if not scratch or not Path(scratch).is_dir():
            self.fail('An existing explicit TMPDIR is required')
        self.temp = tempfile.TemporaryDirectory(prefix='lcd-ownership-', dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.f = Fixture(self.temp.name)
        self.addCleanup(self.f.cleanup)
        self.ns = self.f.load()

    def test_explicit_fixture_and_unexpected_effects_fail(self):
        with self.assertRaises(ValueError):
            load_definitions(None)
        for call in (lambda: self.ns['subprocess'].run(['systemctl', 'reboot']),
                     lambda: self.ns['os'].system('anything'),
                     lambda: self.ns['open']('/run/proxnap-lcd-message.lock', 'a', encoding='utf-8'),
                     lambda: self.ns['page_worker']()):
            with self.assertRaises(AssertionError):
                call()

    def test_transaction_is_nonblocking_exclusive_and_releases(self):
        with self.ns['lcd_transaction'](self.f.path) as owned:
            self.assertTrue(owned)
            self.f.assert_owned()
            with self.ns['lcd_transaction'](self.f.path) as nested:
                self.assertFalse(nested)
        self.f.assert_available()
        self.assertTrue(all(flag == fcntl.LOCK_EX | fcntl.LOCK_NB for flag in self.f.lock_attempts))
        self.assertTrue(all(file.raw.closed for file in self.f.files))

    def test_real_competing_descriptor_denies_then_recovers(self):
        self.f.hold_external()
        with self.ns['lcd_transaction'](self.f.path) as owned:
            self.assertFalse(owned)
        self.f.release_external()
        with self.ns['lcd_transaction'](self.f.path) as owned:
            self.assertTrue(owned)
            self.f.assert_owned()
        self.f.assert_available()

    def test_body_exception_releases_and_is_not_swallowed(self):
        with self.assertRaisesRegex(OSError, 'body failure'):
            with self.ns['lcd_transaction'](self.f.path) as owned:
                self.assertTrue(owned)
                raise OSError('body failure')
        self.f.assert_available()
        with self.ns['lcd_transaction'](self.f.path) as owned:
            self.assertTrue(owned)

    def test_empty_missing_parent_invalid_permission_and_flock_errors_deny(self):
        for path in ('', str(Path(self.temp.name) / 'missing' / 'message.lock'), self.f.path + '\0'):
            with self.subTest(path=path):
                with self.ns['lcd_transaction'](path) as owned:
                    self.assertFalse(owned)
        for name in ('fail_lock_open', 'fail_flock'):
            setattr(self.f, name, True)
            with self.ns['lcd_transaction'](self.f.path) as owned:
                self.assertFalse(owned)
            setattr(self.f, name, False)
        self.assertTrue(all(file.raw.closed for file in self.f.files))
        self.assertEqual(self.f.writes, [])

    def test_render_bytes_both_lines_and_every_delay_under_real_lock(self):
        self.assertEqual(self.f.run(), 0)
        self.assertEqual([data for _, data in self.f.writes], [
            b'\x4d\x0d', b'\x4d\x5e\x01',
            b'\x4d\x0c\x00\x10HOME            ', b'\x4d\x0c\x01\x10SCREEN          '])
        self.assertEqual(self.f.delays, [0.05, 0.05, 0.12, 0.12])
        self.assertIs(self.f.write_owners[-2], self.f.write_owners[-1])
        self.f.assert_available()
        self.assertTrue(self.f.input_closed)
        self.assertEqual(self.f.closed, [901])
        self.assertTrue(self.f.thread_joined)

    def test_startup_and_finally_do_not_write_over_external_owner(self):
        self.f.hold_external()
        self.assertEqual(self.f.run(leave_off=True), 0)
        self.assertEqual(self.f.writes, [])
        self.assertEqual(self.f.reads, [('serial', 0), ('input', 0)])
        self.assertTrue(self.f.input_closed)
        self.assertEqual(self.f.closed, [901])
        self.assertTrue(self.f.thread_joined)

    def test_startup_disabled_under_contention_writes_nothing(self):
        self.f.settings_hook = lambda _tick, settings: setattr(settings, 'enabled', False)
        self.f.hold_external()
        self.f.run(leave_off=True)
        self.assertEqual(self.f.writes, [])
        self.assertEqual(self.f.reads, [])

    def test_config_disabled_transition_and_reenabled_are_guarded(self):
        self.f.times = [10, 16, 22, 28]
        self.f.settings_hook = lambda tick, settings: setattr(settings, 'enabled', tick in {0, 3})
        self.f.hooks[0] = self.f.hold_external
        self.f.hooks[1] = self.f.release_external
        self.f.run()
        self.assertFalse(any(tick == 1 for tick, _ in self.f.writes))
        self.assertIn((2, b'\x4d\x5e\x00'), self.f.writes)
        self.assertIn((3, b'\x4d\x5e\x01'), self.f.writes)
        self.assertIn((3, b'\x4d\x0c\x01\x10SCREEN          '), self.f.writes)

    def test_idle_does_not_blank_external_message(self):
        self.f.times = [10, 400]
        self.f.hooks[0] = self.f.hold_external
        self.f.run()
        self.assertTrue(self.f.writes)
        self.assertFalse(any(tick == 1 for tick, _ in self.f.writes))
        self.assertFalse(any(data == b'\x4d\x28' for _, data in self.f.writes))

    def test_idle_and_wake_protocol_guarded_and_wake_does_not_activate_menu(self):
        self.f.times = [10, 400, 401, 402]
        self.f.events = {2: [1], 3: [0]}
        self.f.run()
        self.assertIn((1, b'\x4d\x28'), self.f.writes)
        self.assertIn((1, b'\x4d\x5e\x00'), self.f.writes)
        self.assertIn((2, b'\x4d\x5e\x01'), self.f.writes)
        self.assertEqual(self.f.ui.mode, 'home')
        self.assertEqual(self.f.actions, [])

    def test_wake_events_under_external_message_never_write(self):
        self.f.times = [10, 400, 401]
        self.f.hooks[1] = self.f.hold_external
        self.f.events = {2: [1, 0]}
        self.f.run()
        self.assertFalse(any(tick == 2 for tick, _ in self.f.writes))
        self.assertEqual(self.f.actions, [])
        self.assertIn(('input', 2), self.f.reads)

    def test_reacquisition_forces_identical_frame_refresh_without_due_tick(self):
        self.f.times = [10, 10.1, 10.2]
        self.f.hooks = {0: self.f.hold_external, 1: self.f.release_external}
        self.f.run()
        lines = [(tick, data) for tick, data in self.f.writes if data.startswith(b'\x4d\x0c')]
        self.assertEqual([tick for tick, _ in lines], [0, 0, 2, 2])
        self.assertEqual(lines[0][1], lines[2][1])
        self.assertEqual(lines[1][1], lines[3][1])

    def test_even_unobserved_external_message_does_not_make_equal_cache_stale(self):
        self.f.times = [10, 11]
        def short_message():
            self.f.hold_external()
            self.f.release_external()
        self.f.hooks[0] = short_message
        self.f.run()
        lines = [(tick, data) for tick, data in self.f.writes if data.startswith(b'\x4d\x0c')]
        self.assertEqual([tick for tick, _ in lines], [0, 0, 1, 1])

    def test_off_uses_configured_lock_and_exact_clear_bytes(self):
        self.assertEqual(self.f.off(), 0)
        self.assertEqual([data for _, data in self.f.writes], [b'\x4d\x28',
            b'\x4d\x0c\x00\x10                ', b'\x4d\x0c\x01\x10                ', b'\x4d\x5e\x00'])
        self.assertEqual(self.f.closed, [901])
        self.f.assert_available()

    def test_off_contention_returns_nonzero_without_serial_open(self):
        self.f.hold_external()
        self.assertEqual(self.f.off(), 1)
        self.assertEqual(self.f.opened, 0)
        self.assertEqual(self.f.writes, [])

    def test_off_lock_errors_never_write(self):
        for bad in ('', str(Path(self.temp.name) / 'missing' / 'message.lock')):
            self.f.settings = self.ns['Settings'](lcd_message_lock=bad)
            self.assertEqual(self.f.off(), 1)
        self.assertEqual(self.f.writes, [])
        self.assertEqual(self.f.opened, 0)

    def test_finally_off_holds_one_lock_and_closes(self):
        self.f.run(leave_off=True)
        self.assertEqual([data for _, data in self.f.writes][-4:], [b'\x4d\x28',
            b'\x4d\x0c\x00\x10                ', b'\x4d\x0c\x01\x10                ', b'\x4d\x5e\x00'])
        self.f.assert_available()
        self.assertTrue(all(file.raw.closed for file in self.f.files))

    def test_new_external_owner_at_exit_blocks_finally_blank(self):
        self.f.hooks[0] = self.f.hold_external
        self.f.run(leave_off=True)
        self.assertFalse(any(data == b'\x4d\x28' for _, data in self.f.writes))

    def test_partial_write_exception_releases_lock_and_closes_resources(self):
        self.f.fail_write = 4
        with self.assertRaisesRegex(OSError, 'UART failure'):
            self.f.run()
        self.f.assert_available()
        self.assertTrue(all(file.raw.closed for file in self.f.files))
        self.assertEqual(self.f.closed, [901])
        self.assertTrue(self.f.input_closed)
        self.assertTrue(self.f.thread_joined)

    def test_startup_open_termios_input_and_worker_failures_cleanup(self):
        for attr, error, expected_closed in (
                ('fail_open', OSError, []), ('fail_attrs', OSError, [901]),
                ('fail_input_open', OSError, [901]), ('fail_thread_start', RuntimeError, [901])):
            with self.subTest(failure=attr):
                f = Fixture(self.temp.name)
                self.addCleanup(f.cleanup)
                f.load()
                setattr(f, attr, True)
                with self.assertRaises(error):
                    f.run()
                self.assertEqual(f.writes, [])
                self.assertEqual(f.closed, expected_closed)
                self.assertTrue(f.input_closed)
                f.assert_available()

    def test_off_open_or_write_failure_still_closes_and_releases(self):
        self.f.fail_attrs = True
        with self.assertRaises(OSError):
            self.f.off()
        self.assertEqual(self.f.closed, [901])
        self.f.assert_available()
        self.f.fail_attrs = False
        self.f.fail_write = 2
        with self.assertRaises(OSError):
            self.f.off()
        self.assertEqual(self.f.closed, [901, 901])
        self.f.assert_available()

    def test_input_close_error_does_not_leak_serial(self):
        self.f.fail_input_close = True
        with self.assertRaises(OSError):
            self.f.run()
        self.assertEqual(self.f.closed, [901])
        self.assertTrue(self.f.thread_joined)
        self.f.assert_available()

    def test_held_power_confirmation_cannot_execute_hidden_or_resume_old_hold(self):
        self.f.times = [10, 11, 14, 15, 16, 17]
        def confirm(ui):
            ui.mode = 'confirm'
            ui.confirm_action = 'Reboot'
        self.f.ui_hook = confirm
        self.f.events = {1: [1], 3: [1], 4: [0]}
        self.f.hooks = {1: self.f.hold_external, 2: self.f.release_external}
        self.f.run()
        self.assertEqual(self.f.actions, [])
        self.assertEqual(self.f.ui.confirm_action, 'Reboot')
        self.assertEqual(self.f.ui.press_mask, 0)

    def test_power_confirmation_threshold_and_mock_command_preserved(self):
        self.f.times = [10, 11, 12.99, 13.0, 14]
        self.f.ui_hook = lambda ui: (setattr(ui, 'mode', 'confirm'), setattr(ui, 'confirm_action', 'Reboot'))
        self.f.events = {1: [1], 4: [0]}
        self.f.run()
        self.assertEqual(self.f.actions, [(3, 'proxnap-lcd-reboot', ['/usr/local/sbin/proxnap-copy-update', '--config', '/etc/proxnap/copy-button.conf', '--reboot'])])
        self.assertEqual(self.f.ui.mode, 'section')

    def test_pending_update_and_fan_ui_never_dispatch_during_external_prompt(self):
        for mode in ('updates', 'fan'):
            with self.subTest(mode=mode):
                f = Fixture(self.temp.name, [10, 11])
                self.addCleanup(f.cleanup)
                f.load()
                f.ui_hook = lambda ui: setattr(ui, 'mode', mode)
                f.events = {0: [1, 0], 1: [2, 0]}
                f.hold_external()
                f.run()
                self.assertEqual(f.actions, [])
                self.assertEqual(f.writes, [])
                f.cleanup()

    def test_run_lock_errors_fail_closed_without_writes_or_actions(self):
        for fault in ('empty', 'missing', 'permission', 'flock'):
            with self.subTest(fault=fault):
                f = Fixture(self.temp.name)
                self.addCleanup(f.cleanup)
                ns = f.load()
                path = '' if fault == 'empty' else (str(Path(self.temp.name) / 'missing' / 'lock') if fault == 'missing' else f.path)
                f.settings = ns['Settings'](lcd_message_lock=path, button_input='offline-buttons')
                f.fail_lock_open = fault == 'permission'
                f.fail_flock = fault == 'flock'
                f.events = {0: [1, 0, 2, 0]}
                f.run(leave_off=True)
                self.assertEqual(f.writes, [])
                self.assertEqual(f.actions, [])
                self.assertTrue(f.input_closed)
                self.assertEqual(f.closed, [901])
                self.assertTrue(all(file.raw.closed for file in f.files))

    def test_lock_error_recovery_reinitializes_without_replaying_input(self):
        self.f.times = [10, 11, 12]
        self.f.fail_flock = True
        self.f.events = {0: [1], 1: [0], 2: [0]}
        self.f.hooks[0] = lambda: setattr(self.f, 'fail_flock', False)
        self.f.run()
        self.assertFalse(any(tick == 0 for tick, _ in self.f.writes))
        self.assertEqual(self.f.writes[0], (1, b'\x4d\x0d'))
        self.assertEqual(self.f.ui.mode, 'home')
        self.f.assert_available()

    def test_config_disabled_startup_has_single_guarded_clear(self):
        self.f.settings_hook = lambda _tick, settings: setattr(settings, 'enabled', False)
        self.f.run()
        self.assertEqual([data for _, data in self.f.writes], [b'\x4d\x0d', b'\x4d\x28',
            b'\x4d\x0c\x00\x10                ', b'\x4d\x0c\x01\x10                ', b'\x4d\x5e\x00'])
        self.assertEqual(self.f.reads, [])

    def test_finally_write_exception_still_releases_and_closes(self):
        self.f.fail_write = 6
        with self.assertRaises(OSError):
            self.f.run(leave_off=True)
        self.f.assert_available()
        self.assertTrue(self.f.input_closed)
        self.assertEqual(self.f.closed, [901])
        self.assertTrue(self.f.thread_joined)

    def test_serial_and_input_events_keep_order_and_existing_menu_gestures(self):
        self.f.times = [10, 11, 12]
        self.f.serial_events = {1: [1]}
        self.f.events = {1: [0], 2: [2, 0]}
        self.f.run()
        self.assertEqual(self.f.ui.mode, 'cover')
        self.assertEqual(self.f.ui.current_group(), 'Network')
        self.assertEqual(self.f.actions, [])
        self.assertEqual(self.f.reads, [('serial', 0), ('input', 0), ('serial', 1), ('input', 1), ('serial', 2), ('input', 2)])

    def test_action_context_redrawn_before_unobserved_external_overlay(self):
        self.f.times = [10, 11, 13]
        self.f.ui_hook = lambda ui: (setattr(ui, 'mode', 'confirm'), setattr(ui, 'confirm_action', 'Shutdown'))
        self.f.events = {1: [1]}
        self.f.hooks[1] = lambda: (self.f.hold_external(), self.f.release_external())
        self.f.run()
        self.assertEqual(self.f.actions, [(2, 'proxnap-lcd-poweroff', ['/usr/local/sbin/proxnap-copy-update', '--config', '/etc/proxnap/copy-button.conf', '--poweroff'])])
        tick_two = [data for tick, data in self.f.writes if tick == 2]
        self.assertEqual(tick_two[0], b'\x4d\x0c\x00\x105.3 Shutdown    ')
        self.assertEqual(tick_two[1], b'\x4d\x0c\x01\x10Hold OK 2s      ')


if __name__ == '__main__':
    unittest.main()
