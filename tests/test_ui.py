from pathlib import Path

from proxnap import lcd, panel_led


def test_lcd_text_is_exactly_sixteen_bytes() -> None:
    assert len(lcd.fit("short")) == 16
    assert lcd.fit("0123456789abcdefghijkl") == b"0123456789abcdef"


def test_panel_settings_are_clamped(tmp_path: Path) -> None:
    config = tmp_path / "panel.conf"
    config.write_text("minimum=-2\nmaximum=200\nperiod_seconds=1\nstep_seconds=0\nstatic_brightness=101\n")
    assert panel_led.settings(str(config)) == (0, 100, 4.0, 0.1, 100)
