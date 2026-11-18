"""Next/Down gestures: actual AST definitions/loop, mandatory inert effects only."""
from pathlib import Path

import pytest

from offline_loader import load_source
from test_lcd_ownership import Fixture
from test_lcd_copy_status import CopyLoopFixture
from test_lcd_power_status import PowerLoopFixture

ROOT = Path(__file__).resolve().parent.parent


def forbidden(*_args, **_kwargs):
    raise AssertionError('Unmocked LCD effect')


@pytest.fixture
def lcd():
    module = load_source(ROOT / 'src/proxnap/lcd.py', 'next_hold_offline')
    # No real process, hardware, worker, device discovery, or final effects.
    module.sh = forbidden
    module.start_systemd_job = forbidden
    module.set_fan_mode = forbidden
    module.current_fan_mode = forbidden
    module.next_fan_mode = forbidden
    module.os.open = forbidden
    module.os.write = forbidden
    module.os.read = forbidden
    return module


@pytest.mark.parametrize('mode', ['home', 'cover', 'section', 'fan', 'updates', 'confirm'])
@pytest.mark.parametrize('duration', [1.0, 1.001, 2.999, 3.0, 3.001, 20.0])
def test_hold_classified_once_on_release_without_intermediate_actions(lcd, mode, duration):
    ui = lcd.LcdUi()
    ui.mode, ui.confirm_action = mode, 'Shutdown'
    ui.home_frame_lock = 2
    ui.override, ui.override_until = ('OLD', 'PROMPT'), 1000
    ui.process_mask(2, 10, 1, [])
    for elapsed in [0.2, 1.0, 1.5, 3.0, 10.0]:
        if elapsed < duration:
            ui.process_mask(2, 10 + elapsed, 1, [])  # autorepeat
            assert not ui.tick(10 + elapsed, 1, [])
            assert ui.mode == mode
            assert not ui.display_off_requested
    ui.process_mask(0, 10 + duration, 1, [])
    assert ui.mode == 'home'
    assert ui.confirm_action is None and ui.override is None
    assert ui.home_frame_lock is None and ui.manual_until == 0
    assert ui.display_off_requested == (duration >= 3)
    assert ui.press_mask == 0
    ui.process_mask(0, 100, 1, [])
    assert not ui.tick(100, 1, [])
    assert ui.mode == 'home'


@pytest.mark.parametrize('duration', [0, 0.001, 0.999])
@pytest.mark.parametrize('mode', ['home', 'cover', 'section', 'updates', 'confirm', 'fan'])
def test_short_navigation_preserved(lcd, mode, duration):
    calls = []
    lcd.next_fan_mode = lambda step: 'quiet'
    lcd.set_fan_mode = lambda value: calls.append(value) or True
    ui = lcd.LcdUi()
    ui.mode, ui.confirm_action = mode, 'Reboot'
    ui.process_mask(2, 10, 2, [])
    ui.process_mask(0, 10 + duration, 2, [])
    assert not ui.display_off_requested
    if mode == 'home':
        assert ui.home_visible_frame == 1
    elif mode == 'cover':
        assert ui.group_index == 1
    elif mode == 'section':
        assert ui.page_by_group['Status'] == 1
    elif mode == 'updates':
        assert ui.update_index == 1
    elif mode == 'confirm':
        assert ui.mode == 'section' and ui.confirm_action is None
    else:
        assert calls == ['quiet']


def test_reset_changed_masks_and_bounce_do_not_replay(lcd):
    ui = lcd.LcdUi()
    ui.process_mask(2, 10, 1, [])
    ui.reset_press()
    ui.process_mask(0, 20, 1, [])
    assert not ui.display_off_requested and ui.home_visible_frame == 0
    for masks in [[2, 1], [2, 3, 2], [3, 2], [2, 255]]:
        for mask in masks:
            ui.process_mask(mask, 30, 1, [])
        ui.process_mask(0, 40, 1, [])
        assert not ui.display_off_requested and ui.home_visible_frame == 0
    ui.process_mask(2, 50, 1, [])
    ui.process_mask(0, 50, 1, [])
    ui.process_mask(2, 50.1, 1, [])
    ui.process_mask(0, 50.1, 1, [])
    assert ui.home_visible_frame == 1


@pytest.mark.parametrize('raw, expected', [('nan', 30), ('inf', 30), ('-inf', 30),
                                         ('junk', 30), ('4', 5), ('121', 120),
                                         ('5', 5), ('120', 120), ('17.5', 17.5)])
def test_home_idle_config_finite_bounds(lcd, tmp_path, raw, expected):
    config = tmp_path / 'lcd.conf'
    config.write_text(f'home_idle_seconds={raw}\n')
    assert lcd.load_settings(str(config)).home_idle_seconds == expected
    assert lcd.load_settings(str(config)).timeout_seconds == 300


def test_default_idle_and_auto_home_frame_rotation(lcd):
    settings = lcd.Settings()
    assert settings.home_idle_seconds == 30 and settings.timeout_seconds == 300
    assert settings.home_frame_seconds == 7 and settings.page_seconds == 8
    ui = lcd.LcdUi()
    ui.enter_menu(10)
    assert ui.manual_until == 40
    ui.home_frame_lock = 1
    ui.go_home(40, resume_cycle=True)
    snapshot = lcd.LiveSnapshot()
    assert [lcd.home_lines(snapshot, now, settings, ui.home_frame_lock)[2]
            for now in [42, 49, 56]] == [0, 1, 2]


def run_fixture(tmp_path, times, *, hook=None, events=None):
    f = Fixture(tmp_path, times)
    f.ui_hook = hook
    f.events = events or {}
    f.load()
    return f


def test_real_loop_off_release_and_dark_wake_no_hidden_action(tmp_path):
    def admin(ui):
        ui.mode, ui.confirm_action = 'confirm', 'Shutdown'
    f = run_fixture(tmp_path, [10, 11, 12, 13.999, 14, 15, 19, 20], hook=admin,
                    events={1: [2], 2: [2], 4: [0], 5: [1], 6: [1], 7: [0]})
    try:
        f.run()
        assert [(tick, data) for tick, data in f.writes if data == b'\x4d\x5e\x00'] == [(4, b'\x4d\x5e\x00')]
        assert (5, b'\x4d\x5e\x01') in f.writes
        assert f.ui.mode == 'home' and not f.actions
        f.assert_available()
    finally:
        f.cleanup()


def test_same_batch_after_off_consumes_wake_instead_of_entering_menu(tmp_path):
    f = run_fixture(tmp_path, [10, 11, 14], events={1: [2], 2: [0, 1, 0]})
    try:
        f.run()
        assert (2, b'\x4d\x5e\x00') in f.writes
        assert f.ui.mode == 'home' and not f.actions
    finally:
        f.cleanup()


@pytest.mark.parametrize('idle', [5, 30, 120])
def test_idle_returns_home_then_off_from_last_activity_not_home_return(tmp_path, idle):
    f = run_fixture(tmp_path, [10, 11, 11 + idle - 0.01, 11 + idle, 311, 311.01],
                    events={1: [1, 0]})
    f.load_settings('offline-config').home_idle_seconds = idle
    observed = []
    f.hooks = {2: lambda: observed.append(f.ui.mode), 3: lambda: observed.append(f.ui.mode)}
    try:
        f.run()
        assert observed == ['cover', 'home']
        assert [(tick, data) for tick, data in f.writes if data == b'\x4d\x5e\x00'] == [(5, b'\x4d\x5e\x00')]
    finally:
        f.cleanup()


def test_idle_does_not_reclassify_active_next_hold_or_change_fan(tmp_path):
    def admin(ui):
        ui.mode, ui.manual_until = 'fan', 12
    f = run_fixture(tmp_path, [10, 11, 12, 13, 14], hook=admin, events={1: [2], 4: [0]})
    f.ns['current_fan_mode'] = lambda: 'auto'  # inert display getter only
    observed = []
    f.hooks = {3: lambda: observed.append(f.ui.mode)}
    try:
        f.run()
        assert observed == ['fan']
        assert (4, b'\x4d\x5e\x00') in f.writes
        assert f.ui.mode == 'home' and not f.actions
    finally:
        f.cleanup()


def test_ownership_handoff_cancels_next_hold_without_later_off(tmp_path):
    f = run_fixture(tmp_path, [10, 11, 12, 14, 15, 16], events={1: [2], 4: [2], 5: [0]})
    f.hooks = {1: f.hold_external, 2: f.release_external}
    try:
        f.run()
        assert not any(data == b'\x4d\x5e\x00' for _, data in f.writes)
        assert not f.actions and f.ui.press_mask == 0
        f.assert_available()
    finally:
        f.cleanup()


@pytest.mark.parametrize('pinned, expected', [(None, 2), (1, 1)])
def test_real_loop_home_rotation_is_not_permanently_pinned(lcd, tmp_path, pinned, expected):
    def home(ui):
        ui.home_visible_frame, ui.home_frame_lock = 1, pinned
    f = run_fixture(tmp_path, [10, 14], hook=home)
    f.ns['home_lines'] = lcd.home_lines
    f.ns['home_frame_index'] = lcd.home_frame_index
    try:
        f.run()
        assert f.ui.home_visible_frame == expected
    finally:
        f.cleanup()


@pytest.mark.parametrize('kind', ['copy', 'power'])
def test_popup_drains_next_hold_without_hidden_off_or_pin_loss(tmp_path, kind):
    fixture = CopyLoopFixture if kind == 'copy' else PowerLoopFixture
    f = fixture(tmp_path, [10, 10.2, 11, 12, 13.2, 14, 15, 16])
    f.ui_hook = lambda ui: setattr(ui, 'home_frame_lock', 2)
    f.events = {2: [2], 4: [2], 6: [0]}
    f.load()
    if kind == 'copy':
        # Exercise UI hold draining with an already-authenticated notice, at any
        # test UID. Real SO_PEERCRED/UID0 integration is covered separately by
        # test_lcd_copy_status, which explicitly requires root.
        class AuthenticatedReceiver:
            name = None

            def configure(self, name, mono):
                self.name = name

            def poll(self, owned):
                notices = f.notices.pop(f.tick, [])
                return notices[0] if owned and notices else None

            def close(self):
                self.name = None

        f.ns['StatusReceiver'] = AuthenticatedReceiver
        f.notices = {1: ['status']}
    else:
        f.power_click(1)
    try:
        f.run()
        assert f.popups == 1
        assert not any(data == b'\x4d\x5e\x00' for _, data in f.writes)
        assert not f.actions and f.ui.home_frame_lock == 2 and f.ui.press_mask == 0
    finally:
        f.cleanup()
