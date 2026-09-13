from pathlib import Path

import pytest

from proxnap import fan_control as fan


def zone() -> fan.FanZone:
    return fan.FanZone("test", "pwm1", (), "system", ((20.0, 30.0), (40.0, 70.0)), 35.0, 80.0)


def test_missing_sensor_forces_full_speed() -> None:
    assert fan.duty_for_temp(None, zone()) == 100.0


def test_critical_temperature_forces_full_speed() -> None:
    assert fan.duty_for_temp(80.0, zone()) == 100.0


def test_curve_interpolates_and_honors_floor() -> None:
    assert fan.duty_for_temp(20.0, zone()) == 35.0
    assert fan.duty_for_temp(30.0, zone()) == pytest.approx(50.0)


def test_pwm_scale() -> None:
    assert fan.duty_to_pwm(0) == 0
    assert fan.duty_to_pwm(100) == 255
    assert fan.duty_to_pwm(50) == 128


def test_fixed_hardware_mapping(tmp_path: Path) -> None:
    config = tmp_path / "fan.conf"
    config.write_text("cpu_min_duty=45\nrear_min_duty=40\n")
    settings = fan.load_settings(str(config))
    assert settings.cpu.pwm == "pwm7"
    assert settings.rear.pwm == "pwm1"
    assert settings.cpu.fan_inputs == ("fan7_input", "fan8_input")
    assert settings.rear.fan_inputs == ("fan1_input", "fan2_input", "fan3_input")


def test_force_full_speed_writes_and_verifies_both_zones(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hwmon = tmp_path / "hwmon0"
    hwmon.mkdir()
    (hwmon / "pwm1").write_text("0")
    (hwmon / "pwm7").write_text("0")
    monkeypatch.setattr(fan, "EC_LOCK", str(tmp_path / "ec.lock"))
    assert fan.force_full_speed(str(hwmon)) is True
    assert (hwmon / "pwm1").read_text() == "255"
    assert (hwmon / "pwm7").read_text() == "255"


def test_any_missing_sensor_forces_both_zones_full(tmp_path: Path) -> None:
    config = tmp_path / "fan.conf"
    config.write_text("")
    settings = fan.load_settings(str(config))
    states = {"cpu": fan.ZoneState(), "rear": fan.ZoneState()}
    requested, missing = fan.requested_duties(
        settings, {"cpu": None, "rear": 40.0}, states
    )
    assert missing is True
    assert requested == {"cpu": 100.0, "rear": 100.0}


def test_invalid_sensor_policy_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "fan.conf"
    config.write_text("cpu_sensor_policy=arbitrary\n")
    with pytest.raises(ValueError, match="unsupported sensor policy"):
        fan.load_settings(str(config))


def test_one_of_multiple_fans_stalled_triggers_zone_failure(tmp_path: Path) -> None:
    hwmon = tmp_path / "hwmon0"
    hwmon.mkdir()
    for name, rpm in (("fan1_input", "1000"), ("fan2_input", "0"), ("fan3_input", "900")):
        (hwmon / name).write_text(rpm)
    item = fan.FanZone(
        "rear",
        "pwm1",
        ("fan1_input", "fan2_input", "fan3_input"),
        "storage",
        ((20.0, 40.0), (70.0, 100.0)),
        40.0,
        70.0,
    )
    state = fan.ZoneState()
    assert fan.rpm_stalled(str(hwmon), item, state, 40.0, 3) is False
    assert fan.rpm_stalled(str(hwmon), item, state, 40.0, 3) is False
    assert fan.rpm_stalled(str(hwmon), item, state, 40.0, 3) is True
    assert state.failed_rpm_cycles["fan2_input"] == 3


def test_missing_expected_tachometer_triggers_zone_failure(tmp_path: Path) -> None:
    hwmon = tmp_path / "hwmon0"
    hwmon.mkdir()
    (hwmon / "fan7_input").write_text("1200")
    item = fan.FanZone(
        "cpu",
        "pwm7",
        ("fan7_input", "fan8_input"),
        "cpu_package",
        ((20.0, 50.0), (90.0, 100.0)),
        50.0,
        92.0,
    )
    state = fan.ZoneState()
    assert fan.rpm_stalled(str(hwmon), item, state, 50.0, 2) is False
    assert fan.rpm_stalled(str(hwmon), item, state, 50.0, 2) is True
    assert state.failed_rpm_cycles["fan8_input"] == 2


def test_recovered_fan_resets_only_its_counter(tmp_path: Path) -> None:
    hwmon = tmp_path / "hwmon0"
    hwmon.mkdir()
    (hwmon / "fan1_input").write_text("0")
    (hwmon / "fan2_input").write_text("0")
    item = fan.FanZone(
        "rear",
        "pwm1",
        ("fan1_input", "fan2_input"),
        "storage",
        ((20.0, 40.0), (70.0, 100.0)),
        40.0,
        70.0,
    )
    state = fan.ZoneState()
    assert fan.rpm_stalled(str(hwmon), item, state, 40.0, 3) is False
    (hwmon / "fan1_input").write_text("800")
    assert fan.rpm_stalled(str(hwmon), item, state, 40.0, 3) is False
    assert state.failed_rpm_cycles == {"fan1_input": 0, "fan2_input": 2}
