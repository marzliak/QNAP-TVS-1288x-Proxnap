"""Actual LCD loop/popup over unique abstract IPC, inert UART/UI/effect IO."""
import os
from pathlib import Path
import socket
import tempfile
import unittest
import uuid

from offline_loader import load_source
from proxnap import status_ipc as ipc
from test_copy_button import Harness, clicks
from test_lcd_power_status import PowerLoopFixture, status

ROOT = Path(__file__).resolve().parents[1]


class CopyLoopFixture(PowerLoopFixture):
    def __init__(self, root, times):
        super().__init__(root, times)
        self.endpoint = "@proxnap-lcd-offline-" + uuid.uuid4().hex
        self.notices = {}
        self.receivers = []
        self.copy_hook = None

    def load_settings(self, config):
        settings = super().load_settings(config)
        settings.power_status_enabled = False
        settings.power_status_page_seconds = 3.0
        settings.copy_status_enabled = True
        settings.copy_status_socket = self.endpoint
        if self.copy_hook:
            self.copy_hook(self.tick, settings)
        return settings

    def load(self):
        ns = super().load()
        fixture = self

        class Receiver(ipc.StatusReceiver):
            def __init__(self):
                super().__init__()
                fixture.receivers.append(self)

            def configure(self, name, mono):
                super().configure(name, mono)
                for operation in fixture.notices.pop(fixture.tick, []):
                    assert ipc.send_status_notice(name, operation)

        ns['StatusReceiver'] = Receiver
        return ns


@unittest.skipIf(os.geteuid() != 0, 'real UID0 socket/popup integration requires root; inert protocol tests run everywhere')
class CopyLcdTests(unittest.TestCase):
    def setUp(self):
        scratch = os.environ['TMPDIR']
        self.temp = tempfile.TemporaryDirectory(prefix='lcd-copy-loop-', dir=scratch)
        self.addCleanup(self.temp.cleanup)

    def fixture(self, times):
        f = CopyLoopFixture(self.temp.name, times)
        self.addCleanup(f.cleanup)
        f.load()
        return f

    def run_fixture(self, f):
        self.assertEqual(f.run(), 0)
        self.assertTrue(f.thread_joined and f.input_closed)
        self.assertEqual(f.actions, [])
        self.assertTrue(all(r.channel is None for r in f.receivers))
        f.assert_available()

    def test_wakes_dark_lcd_keeps_home_lock_and_returns_after_three_seconds(self):
        f = self.fixture([10, 400, 401, 402, 403.9, 404.1])
        f.ui_hook = lambda ui: (setattr(ui, 'home_frame_lock', 2), setattr(ui, 'home_visible_frame', 2))
        f.notices = {2: ['status']}
        self.run_fixture(f)
        self.assertEqual(f.popups, 1)
        self.assertEqual(f.ui.home_frame_lock, 2)
        self.assertEqual(f.ui.mode, 'home')
        self.assertIn((1, b'\x4d\x5e\x00'), f.writes)
        self.assertIn((2, b'\x4d\x5e\x01'), f.writes)
        self.assertTrue(any(t == 4 and b'pool degraded' in data for t, data in f.writes))
        self.assertTrue(any(t == 5 and b'HOME' in data for t, data in f.writes))

    def test_cancel_restores_without_replaying_held_navigation_or_admin_confirm(self):
        f = self.fixture([10, 10.2, 10.4, 10.6, 10.8, 11, 11.2])
        f.ui_hook = lambda ui: (setattr(ui, 'mode', 'confirm'), setattr(ui, 'confirm_action', 'Reboot'))
        f.notices = {1: ['status'], 3: ['cancel']}
        f.events = {2: [1], 3: [1], 4: [1], 5: [0]}
        self.run_fixture(f)
        self.assertEqual(f.popups, 1)
        self.assertEqual(f.ui.mode, 'home')
        self.assertIsNone(f.ui.confirm_action)
        self.assertEqual(f.ui.press_mask, 0)
        self.assertFalse(any(t >= 3 and b'pool degraded' in data for t, data in f.writes))

    def test_no_ownership_discards_status_and_cancel_never_deferred(self):
        f = self.fixture([10, 10.2, 10.4, 10.6, 10.8, 11, 14.1])
        f.hooks = {0: f.hold_external, 2: f.release_external}
        f.notices = {1: ['status'], 2: ['cancel'], 3: ['status'], 4: ['status']}
        self.run_fixture(f)
        self.assertEqual(f.popups, 1)
        self.assertFalse(any(t in {1, 2} for t, _data in f.writes))
        self.assertFalse(any(t <= 3 and b'pool degraded' in data for t, data in f.writes))

    def test_external_writer_interrupts_popup_and_later_cancel_does_not_resume_it(self):
        f = self.fixture([10, 10.2, 10.4, 10.6, 10.8, 11])
        f.notices = {1: ['status'], 2: ['cancel']}
        f.hooks = {1: f.hold_external, 2: f.release_external}
        self.run_fixture(f)
        self.assertEqual(f.popups, 1)
        self.assertFalse(any(t >= 2 and b'pool degraded' in data for t, data in f.writes))

    def test_disabled_receiver_and_endpoint_reload_close_previous_popup(self):
        for enabled in (False, True):
            f = self.fixture([10, 11, 16, 22])
            original_endpoint = f.endpoint
            replacement = "@proxnap-lcd-offline-" + uuid.uuid4().hex
            def reconfigure(tick, settings):
                if tick >= 2:
                    settings.copy_status_enabled = enabled
                    settings.copy_status_socket = replacement
            f.copy_hook = reconfigure
            f.notices = {1: ['status']}
            self.run_fixture(f)
            self.assertEqual(f.popups, 1)
            self.assertFalse(ipc.send_status_notice(original_endpoint, 'status'))
            self.assertFalse(any(t >= 2 and b'pool degraded' in data for t, data in f.writes))

    def test_bind_failure_leaves_regular_lcd_working(self):
        f = self.fixture([10, 10.2, 10.4])
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as blocker:
            blocker.bind(ipc._address(f.endpoint))
            self.run_fixture(f)
        self.assertEqual(f.popups, 0)
        self.assertTrue(any(b'HOME' in data for _tick, data in f.writes))

    def test_unknown_and_critical_share_same_existing_snapshot_parser(self):
        for payload, expected in [(None, b'Desconhecido'),
                                  (status(timestamp=1010, status='critical', reasons=['disk failed'], unavailable=['smart']), b'disk failed')]:
            f = self.fixture([10, 10.2, 10.4, 14])
            original = f.ns['load_settings']
            def configured(config):
                settings = original(config)
                f.power.payload = '{malformed' if payload is None else payload
                return settings
            f.ns['load_settings'] = configured
            f.notices = {1: ['status']}
            self.run_fixture(f)
            self.assertTrue(any(expected in data for _tick, data in f.writes))

    def test_all_status_receiver_resources_close_on_serial_error(self):
        f = self.fixture([10, 10.2])
        f.notices = {1: ['status']}
        f.write_hook = lambda _data: (_ for _ in ()).throw(OSError('offline write')) if f.tick == 1 else None
        with self.assertRaises(OSError):
            f.run()
        self.assertTrue(all(r.channel is None for r in f.receivers))
        self.assertTrue(f.input_closed and f.thread_joined)
        f.assert_available()


    def test_copy_parser_to_real_socket_to_existing_popup_end_to_end(self):
        f = self.fixture([10, 10.2, 10.4, 13.3])
        original = f.ns['StatusReceiver'].configure
        copy_source = (ROOT / 'src/proxnap/copy_update.py').read_text()
        def configure(receiver, name, now):
            original(receiver, name, now)
            if f.tick == 1:
                copy = Harness(copy_source, clicks(101, 1) + [(103.1, BlockingIOError())],
                               single_click_status_enabled=True, status_socket=name)
                copy.ns['send_status_notice'] = ipc.send_status_notice
                self.assertEqual(copy.run(), [])
                self.assertEqual(copy.effects, [])
        f.ns['StatusReceiver'].configure = configure
        self.run_fixture(f)
        self.assertEqual(f.popups, 1)
        self.assertTrue(any(t == 1 and b'pool degraded' in data for t, data in f.writes))
        self.assertTrue(any(t == 3 and b'HOME' in data for t, data in f.writes))


def test_configuration_defaults_optins_and_update_only_path(tmp_path):
    # AST definitions only; load_source refuses writes/reads outside scratch.
    lcd = load_source(ROOT / 'src/proxnap/lcd.py', 'copy_lcd_config_offline')
    copy = load_source(ROOT / 'src/proxnap/copy_update.py', 'copy_status_config_offline')
    conf = tmp_path / 'lcd.conf'
    conf.write_text('')
    settings = lcd.load_settings(str(conf))
    assert not settings.copy_status_enabled
    assert settings.copy_status_socket == '@proxnap-lcd-status'
    assert settings.copy_update_config == lcd.COPY_UPDATE_CONFIG
    cfg = copy.load_settings(str(conf))
    assert not cfg.single_click_status_enabled and cfg.status_socket == '@proxnap-lcd-status'
    conf.write_text('copy_status_enabled=true\ncopy_status_socket=@fixture-lcd\ncopy_update_config=/offline/custom-copy.conf\n')
    settings = lcd.load_settings(str(conf))
    assert settings.copy_status_enabled and settings.copy_status_socket == '@fixture-lcd'
    calls = []
    lcd.start_systemd_job = lambda unit, args: calls.append((unit, args)) or True
    ui = lcd.LcdUi()
    ui.settings = settings
    for action, flag, config in [
        ('UpdateRefreshRun', '--run-upgrade', settings.copy_update_config),
        ('UpdateRefresh', '--check-updates', settings.copy_update_config),
        ('UpdateRun', '--run-install', settings.copy_update_config),
        ('Reboot', '--reboot', lcd.COPY_UPDATE_CONFIG),
        ('Shutdown', '--poweroff', lcd.COPY_UPDATE_CONFIG),
    ]:
        ui.confirm_action = action
        ui.execute_confirm(10)
        assert calls[-1][1] == [lcd.COPY_UPDATE_CONTROL, '--config', config, flag]
    conf.write_text('single_click_status_enabled=true\nstatus_socket=@fixture-lcd\n')
    cfg = copy.load_settings(str(conf))
    assert cfg.single_click_status_enabled and cfg.status_socket == '@fixture-lcd'
    assert not cfg.reboot_enabled and not cfg.poweroff_enabled and not cfg.upgrade_enabled
