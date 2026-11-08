"""Opt-in LCD presentation: real AST renderer/loop, fake time, mandatory inert IO."""
import math

import pytest

from offline_loader import load_source
import test_lcd_copy_status as copy_tests
from test_lcd_power_status import PowerIO, load_power, status, ALIAS_A, ALIAS_B


@pytest.fixture
def renderer(tmp_path):
    io = PowerIO()
    ns = load_power(io)
    # Real Settings/UI definitions, with every effect blocked by the AST loader.
    module = load_source(copy_tests.ROOT / 'src/proxnap/lcd.py', 'scroll_settings_offline')
    settings = module.Settings(status_scroll_enabled=True)
    return io, ns, module, settings


def popup(renderer, payload=None, **changes):
    io, ns, module, settings = renderer
    if payload is not None:
        io.payload = payload
    for key, value in changes.items():
        setattr(settings, key, value)
    ui = module.LcdUi()
    ui.home_frame_lock = 2
    ui.home_visible_frame = 2
    return ns['_PowerStatusPopup'](ui, 0.0, 3.0, settings), ui


@pytest.mark.parametrize('length', [1, 15, 16, 17, 18, 30])
def test_initial_step_final_window_and_complete_end_pause(renderer, length):
    body = 'abcdefghijklmnopqrstuvwxyz0123456789'[:length]
    p, _ = popup(renderer, status(status='critical', reasons=[body]))
    steps = max(0, length - 16)
    duration = 3 + max(0, steps - 1) * .6
    assert p.timeline[-1][0] == pytest.approx(duration)
    assert p.lines(0) == ('CRITICO 1/1', body[:16])
    assert p.lines(1.5 - 1e-6) == p.lines(0)
    if steps:
        for offset in range(1, steps + 1):
            at = 1.5 + (offset - 1) * .6
            assert p.lines(at)[1] == body[offset:offset + 16]
        last_at = 1.5 + (steps - 1) * .6
        assert duration - last_at == pytest.approx(1.5)
    assert p.lines(duration - 1e-6)[1] == body[-16:]
    assert p.lines(duration) is None
    assert p.lines(1000) is None  # no loop/restart


def test_automatic_causes_fixed_header_and_home_frame(renderer):
    p, ui = popup(renderer, status(status='critical', reasons=['primeiro', 'segundo']))
    assert p.lines(2.999) == ('CRITICO 1/2', 'primeiro')
    assert p.lines(3) == ('CRITICO 2/2', 'segundo')
    assert p.lines(5.999) == ('CRITICO 2/2', 'segundo')
    assert p.lines(6) is None
    ui.mode, ui.confirm_action, ui.press_mask = 'section', 'Reboot', 1
    p.finish(ui, 6)
    assert ui.mode == 'home' and ui.confirm_action is None and ui.press_mask == 0
    assert (ui.home_frame_lock, ui.home_visible_frame) == (2, 2)
    assert ui.manual_until == 0


@pytest.mark.parametrize('payload,expected', [
    (status(), [('ESTADO ATUAL', 'Tudo OK')]),
    (status(status='warning', warnings=['update running', '3 security updates', 'reboot required']),
     [('AVISO', 'Atualizacao em andamento'), ('AVISO', '3 atualizacoes de seguranca'), ('AVISO', 'Reinicio necessario')]),
    (status(status='critical', reasons=['root filesystem is read-only'], unavailable=['configured disk unavailable: bay-a']),
     [('CRITICO', 'Sistema raiz somente leitura')]),
    (status(status='unknown', unavailable=['configured disk unavailable: bay-a', 'configured disk unavailable: bay-b']),
     [('SEM DADOS', 'Disco configurado indisponivel: bay-a'), ('SEM DADOS', 'Disco configurado indisponivel: bay-b')]),
    (status(status='critical', reasons=['ação\x00requerida 🙂']), [('CRITICO', 'acao requerida ?')]),
    (status(status='critical', reasons=['source-specific: faithful unrecognized detail']),
     [('CRITICO', 'source-specific: faithful unrecognized detail')]),
])
def test_meaningful_portuguese_priority_unicode_and_faithful_fallback(renderer, payload, expected):
    io, ns, _, settings = renderer
    io.payload = payload
    assert ns['_power_status_pages'](settings) == expected
    assert io.payload == payload  # presentation never edits the producer snapshot


@pytest.mark.parametrize('payload', [
    '{', '[]', 'x' * 65537, status(timestamp=69), status(timestamp=101),
    status(timestamp=True), status(check_seconds=float('nan')),
    status(status='ok', reasons=['broken']), status(reasons=None),
    status(status='critical'), status(status='warning'), status(status='unexpected'),
    status(status='critical', reasons=['problem'], unavailable=[None]),
])
def test_invalid_stale_incoherent_never_green(renderer, payload):
    p, _ = popup(renderer, payload)
    assert p.pages == [('SEM DADOS', 'Dados indisponiveis')]
    assert p.lines(0) == ('SEM DADOS 1/1', 'Dados indisponiv')
    assert all('OK' not in text for _, _, text in p.timeline)


def test_exact_private_alias_lookup_per_failed_unit_without_historical_inference(renderer):
    io, ns, _, settings = renderer
    settings.status_labels = {'example-prepare.service': 'Falha ao preparar o driver',
                              'example-other.service': 'Outra tarefa'}
    raw = status(status='critical', reasons=[
        'failed systemd units: example-prepare.service, example-prepare.timer, example-other.service'])
    io.payload = raw
    assert ns['_power_status_pages'](settings) == [
        ('CRITICO', 'Falha ao preparar o driver'),
        ('CRITICO', 'Servico em falha: example-prepare.timer'), ('CRITICO', 'Outra tarefa')]
    for token in ['EXAMPLE-prepare.service', 'example-prepare.service ', '../example-prepare.service']:
        io.payload = status(status='critical', reasons=['failed systemd units: ' + token])
        text = ns['_power_status_pages'](settings)[0][1]
        assert 'Falha ao preparar' not in text
        assert token.strip() in text
    assert io.status_reads == 4


@pytest.mark.parametrize('number,length,maximum,pause', [
    (32, 1, 60, 1.5), (33, 1, 60, 1.5), (1, 512, 60, 1.5),
    (1, 513, 60, 1.5), (2, 70, 10, 1.5), (32, 512, 60, 5),
    (2, 17, 10, 5),
])
def test_caps_budget_explicit_omission_and_notice_end_pause(renderer, number, length, maximum, pause):
    p, _ = popup(renderer, status(status='critical', reasons=['x' * length] * number),
                 status_scroll_max_seconds=maximum, status_scroll_pause_seconds=pause)
    assert 0 < p.timeline[-1][0] <= maximum
    assert len(p.timeline) <= 32
    assert all(len(text) <= 512 and len(title) <= 16 for _, title, text in p.timeline)
    assert p.timeline[-1][2] == 'Mais...'
    notice_start = p.timeline[-2][0] if len(p.timeline) > 1 else 0
    end = p.timeline[-1][0]
    assert end - notice_start == pytest.approx(2 * pause)
    assert p.lines(notice_start) == ('CRITICO MAIS', 'Mais...')
    assert p.lines(end - 1e-6) == p.lines(notice_start)
    assert p.lines(end) is None
    assert all(title.endswith('+') for _, title, _ in p.timeline[:-1])


def test_maximum_exact_fit_no_false_truncation_and_direct_settings_safety(renderer):
    p, _ = popup(renderer, status(status='critical', reasons=['a'] * 20))
    assert len(p.timeline) == 20 and p.timeline[-1][0] == 60
    assert p.timeline[-1][1:] == ('CRITICO 20/20', 'a')
    p, _ = popup(renderer, status(status='critical', reasons=['b' * 512]),
                 status_scroll_step_seconds=float('nan'), status_scroll_pause_seconds=float('inf'),
                 status_scroll_max_seconds=10000)
    assert (p.step, p.pause) == (.6, 1.5) and p.timeline[-1][0] <= 60


def test_configuration_defaults_clamps_finite_and_literal_label_keys(renderer, tmp_path):
    _, _, module, _ = renderer
    config = tmp_path / 'scroll.conf'
    config.write_text('')
    settings = module.load_settings(str(config))
    assert not settings.status_scroll_enabled and settings.status_labels == {}
    assert (settings.status_scroll_step_seconds, settings.status_scroll_pause_seconds,
            settings.status_scroll_max_seconds) == (.6, 1.5, 60)
    for value in ['nan', 'inf', '-inf', 'bad', '1e999']:
        config.write_text('status_scroll_enabled=true\n' + '\n'.join(
            key + '=' + value for key in ['status_scroll_step_seconds', 'status_scroll_pause_seconds',
                                          'status_scroll_max_seconds']))
        settings = module.load_settings(str(config))
        assert settings.status_scroll_enabled
        assert (settings.status_scroll_step_seconds, settings.status_scroll_pause_seconds,
                settings.status_scroll_max_seconds) == (.6, 1.5, 60)
    config.write_text('status_scroll_step_seconds=-2\nstatus_scroll_pause_seconds=100\nstatus_scroll_max_seconds=999\n'
                      'status_label.example-task.service=Falha na tarefa\n'
                      'status_label.example-task.timer=Timer\n'
                      'status_label.../example-task.service=Invalid\n'
                      'status_label.example-task.service.extra=Invalid\n'
                      'status_label.example-task .service=Invalid\n'
                      'status_label.example-spaced.service =Invalid\n'
                      'status_label.example-task=Invalid\n'
                      'status_label.example-empty.service=\n'
                      'status_label.example-control.service=bad\x00label\n'
                      'status_label.example-long.service=' + 'x' * 513 + '\n'
                      r'status_label.example\x2dworker@a.service=Escaped instance' + '\n')
    settings = module.load_settings(str(config))
    assert (settings.status_scroll_step_seconds, settings.status_scroll_pause_seconds,
            settings.status_scroll_max_seconds) == (.6, 5, 60)
    assert settings.status_labels == {'example-task.service': 'Falha na tarefa', 'example-task.timer': 'Timer',
                                      r'example\x2dworker@a.service': 'Escaped instance'}
    for token in [' example-task.service', 'example-task.service ', '../example-task.service', 'bad/service', '',
                  'a' * 256 + '.service', r'example\xZZ.service']:
        assert not module._status_unit_id(token)
    assert module._status_seconds(None, .6, .6, 2) == .6
    assert math.isfinite(settings.status_scroll_max_seconds)


def test_legacy_constructor_and_off_setting_keep_exact_pager(renderer):
    io, ns, module, settings = renderer
    io.payload = status(status='critical', reasons=['a moderately long untranslated reason'])
    ui = module.LcdUi()
    ui.mode, ui.manual_until = 'section', 20
    legacy = ns['_PowerStatusPopup'](ui, 0, 3)
    settings.status_scroll_enabled = False
    disabled = ns['_PowerStatusPopup'](ui, 0, 3, settings)
    assert legacy.pages == disabled.pages == ns['_power_status_pages']()
    for at in [0, 2.999, 3, 5.999, 6, 100]:
        assert legacy.lines(at) == disabled.lines(at)
    disabled.finish(ui, 10)
    assert ui.mode == 'section' and ui.manual_until == 30


class ScrollLoopTests(copy_tests.CopyLcdTests):
    """Run every existing Copy/IPC/ownership regression again with scrolling on."""

    def fixture(self, times):
        fixture = super().fixture(times)
        load = fixture.ns['load_settings']

        def configured(config):
            settings = load(config)
            settings.status_scroll_enabled = True
            return settings

        fixture.ns['load_settings'] = configured
        return fixture

    def test_wakes_dark_lcd_keeps_home_lock_and_returns_after_three_seconds(self):
        f = self.fixture([10, 400, 401, 402, 403.9, 404.1])
        f.ui_hook = lambda ui: (setattr(ui, 'home_frame_lock', 2), setattr(ui, 'home_visible_frame', 2))
        f.notices = {2: ['status']}
        self.run_fixture(f)
        assert f.popups == 1 and f.ui.home_frame_lock == 2 and f.ui.mode == 'home'
        assert (1, b'\x4d\x5e\x00') in f.writes and (2, b'\x4d\x5e\x01') in f.writes
        assert any(t == 2 and b'pool degraded' in data for t, data in f.writes)
        assert not any(t in {3, 4} for t, _ in f.writes)  # pause does not refresh equal frames
        assert any(t == 5 and b'HOME' in data for t, data in f.writes)

    def test_unknown_and_critical_share_same_existing_snapshot_parser(self):
        for payload, expected in [(None, b'Dados indisponiv'),
                                  (status(timestamp=1010, status='critical', reasons=['disk failed'],
                                          unavailable=['configured disk unavailable: bay-a']), b'disk failed')]:
            f = self.fixture([10, 10.2, 10.4, 14])
            load = f.ns['load_settings']

            def configured(config):
                settings = load(config)
                f.power.payload = '{malformed' if payload is None else payload
                return settings

            f.ns['load_settings'] = configured
            f.notices = {1: ['status']}
            self.run_fixture(f)
            assert any(expected in data for _, data in f.writes)

    def test_changed_frames_only_original_serial_protocol_and_fresh_navigation(self):
        f = self.fixture([10, 10.2, 10.4, 11.69, 11.7, 11.9, 12.3, 13.2, 13.4, 13.8])
        load = f.ns['load_settings']

        def configured(config):
            settings = load(config)
            f.power.payload = status(timestamp=1010, status='critical', reasons=['abcdefghijklmnopq'])
            return settings

        f.ns['load_settings'] = configured
        f.notices = {1: ['status'], 2: ['status']}  # never restart the timeline
        f.ui_hook = lambda ui: (setattr(ui, 'mode', 'confirm'), setattr(ui, 'confirm_action', 'Shutdown'),
                                setattr(ui, 'home_frame_lock', 2))
        f.events = {2: [1], 7: [1], 8: [0], 9: [1, 0]}
        self.run_fixture(f)
        assert f.popups == 1 and f.power.status_reads == 1
        assert not any(t in {2, 3, 5, 6} for t, _ in f.writes)
        assert (1, b'\x4d\x0c\x00\x10CRITICO 1/1     ') in f.writes
        assert (4, b'\x4d\x0c\x01\x10bcdefghijklmnopq') in f.writes
        assert any(t == 7 and b'HOME' in data for t, data in f.writes)
        assert f.ui.home_frame_lock == 2 and f.ui.mode == 'cover' and f.actions == []

    def test_unknown_configured_disks_are_not_healthy_or_failed_hardware(self):
        f = self.fixture([10, 10.2, 26, 42, 58, 71])
        load = f.ns['load_settings']
        payload = status(timestamp=1010, status='unknown', unavailable=[
            'configured disk unavailable: bay-a', 'configured disk unavailable: bay-b',
            'configured disk unavailable: bay-c'])

        def configured(config):
            settings = load(config)
            f.power.payload = payload
            return settings

        f.ns['load_settings'] = configured
        f.notices = {1: ['status']}
        self.run_fixture(f)
        assert f.popups == 1 and f.power.payload == payload
        assert any(b'SEM DADOS 1/3' in data for _, data in f.writes)
        assert any(b'SEM DADOS 2/3' in data for _, data in f.writes)
        assert any(b'SEM DADOS 3/3' in data for _, data in f.writes)
        assert not any(b'Tudo OK' in data or b'defeituoso' in data for _, data in f.writes)
        assert any(t == 5 and b'HOME' in data for t, data in f.writes)

    def test_power_rebind_cancels_only_its_popup_no_replayed_actions(self):
        f = self.fixture([10, 11, 16, 22, 28])
        f.copy_hook = lambda tick, settings: (
            setattr(settings, 'power_status_enabled', True),
            setattr(settings, 'power_status_inputs', (ALIAS_B,) if tick >= 2 else (ALIAS_A,)))
        f.power_click(1)
        f.events = {1: [1], 2: [1], 3: [0]}
        self.run_fixture(f)
        assert f.popups == 1
        assert not any(t >= 2 and b'CRITICO' in data for t, data in f.writes)
        assert f.ui.mode == 'home' and f.ui.press_mask == 0

    def test_copy_press_cancels_any_information_but_power_rebind_does_not_cancel_copy(self):
        for source in ['power', 'copy']:
            f = self.fixture([10, 11, 16, 17, 30])
            f.copy_hook = lambda tick, settings: (
                setattr(settings, 'power_status_enabled', True),
                setattr(settings, 'power_status_inputs', (ALIAS_B,) if tick >= 2 else (ALIAS_A,)))
            load = f.ns['load_settings']

            def configured(config):
                settings = load(config)
                f.power.payload = status(timestamp=1010, status='critical', reasons=['abcdefghijklmnopqrstuvwxyz0123456789ABCD'])
                return settings

            f.ns['load_settings'] = configured
            if source == 'power':
                f.power_click(1)
                f.notices = {2: ['cancel']}
                # Keep Power binding stable for this independent source test.
                f.copy_hook = lambda _tick, settings: (setattr(settings, 'power_status_enabled', True),
                                                       setattr(settings, 'power_status_inputs', (ALIAS_A,)))
            else:
                f.notices = {1: ['status']}
            self.run_fixture(f)
            assert f.popups == 1
            if source == 'power':
                # Any fresh Copy press must reveal later maintenance prompts,
                # even if the informational popup came from another source.
                assert not any(t >= 2 and b'CRITICO' in data for t, data in f.writes)
            else:
                assert any(t == 2 and b'CRITICO' in data for t, data in f.writes)
            assert f.ui.mode == 'home' and f.actions == []
