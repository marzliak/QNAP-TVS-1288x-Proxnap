from pathlib import Path

from proxnap import common


def test_read_config_strips_comments(tmp_path: Path) -> None:
    path = tmp_path / "sample.conf"
    path.write_text("enabled=true # comment\nignored\nvalue = 42\n")
    assert common.read_config(str(path)) == {"enabled": "true", "value": "42"}


def test_set_config_value_preserves_real_newlines_and_other_keys(tmp_path: Path) -> None:
    path = tmp_path / "button.conf"
    path.write_text("enabled=false\nreboot_enabled=false\n# keep this comment\n")
    common.set_config_value(str(path), "enabled", "true")
    common.set_config_value(str(path), "poweroff_enabled", "true")
    content = path.read_text()
    assert "\\n" not in content
    assert content.endswith("\n")
    assert content.splitlines() == [
        "enabled=true",
        "reboot_enabled=false",
        "# keep this comment",
        "poweroff_enabled=true",
    ]
    assert common.read_config(str(path)) == {
        "enabled": "true",
        "reboot_enabled": "false",
        "poweroff_enabled": "true",
    }


def test_bool_parsing_is_fail_closed() -> None:
    assert common.parse_bool("yes") is True
    assert common.parse_bool("off", True) is False
    assert common.parse_bool("unexpected") is False


def test_exclusive_lock_creates_file(tmp_path: Path) -> None:
    path = tmp_path / "locks" / "ec.lock"
    with common.exclusive_lock(str(path)):
        assert path.exists()


def test_model_identity_accepts_exact_dmi(tmp_path: Path) -> None:
    dmi = tmp_path / "product_name"
    dmi.write_text("TVS-h1288X\n")
    assert common.model_is_supported(str(dmi), str(tmp_path / "missing-vpd")) is True


def test_model_identity_accepts_tested_vpd_pair(tmp_path: Path) -> None:
    dmi = tmp_path / "product_name"
    dmi.write_text("Default string\n")
    vpd = tmp_path / "vpd"
    vpd.mkdir()
    (vpd / "mainboard_model").write_text("Q05W0\n")
    (vpd / "backplane_model").write_text("Q05K0\n")
    assert common.model_is_supported(str(dmi), str(vpd)) is True


def test_model_identity_rejects_untested_1688x(tmp_path: Path) -> None:
    dmi = tmp_path / "product_name"
    dmi.write_text("TVS-h1688X\n")
    vpd = tmp_path / "vpd"
    vpd.mkdir()
    (vpd / "mainboard_model").write_text("Q05T0\n")
    (vpd / "backplane_model").write_text("Q0630\n")
    assert common.model_is_supported(str(dmi), str(vpd)) is False


def test_explicit_1688x_dmi_overrides_conflicting_1288x_vpd(tmp_path: Path) -> None:
    dmi = tmp_path / "product_name"
    dmi.write_text("TVS-h1688X\n")
    vpd = tmp_path / "vpd"
    vpd.mkdir()
    (vpd / "mainboard_model").write_text("Q05W0\n")
    (vpd / "backplane_model").write_text("Q05K0\n")
    assert common.model_is_supported(str(dmi), str(vpd)) is False


def test_specific_unsupported_dmi_cannot_use_1288x_vpd_fallback(tmp_path: Path) -> None:
    dmi = tmp_path / "product_name"
    dmi.write_text("UNRELATED-SERVER\n")
    vpd = tmp_path / "vpd"
    vpd.mkdir()
    (vpd / "mainboard_model").write_text("Q05W0\n")
    (vpd / "backplane_model").write_text("Q05K0\n")
    assert common.model_is_supported(str(dmi), str(vpd)) is False
