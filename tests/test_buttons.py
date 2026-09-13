from pathlib import Path

import pytest

from proxnap import buttons


def policy(**overrides: object) -> buttons.Policy:
    values: dict[str, object] = {
        "enabled": True,
        "input_paths": (),
        "key_code": buttons.BTN_COPY,
        "refresh_count": 3,
        "upgrade_count": 5,
        "reboot_count": 8,
        "poweroff_count": 12,
        "confirm_count": 3,
        "sequence_window": 5.0,
        "settle_seconds": 1.0,
        "confirm_window": 5.0,
        "apt_refresh_enabled": False,
        "apt_upgrade_enabled": False,
        "reboot_enabled": False,
        "poweroff_enabled": False,
    }
    values.update(overrides)
    return buttons.Policy(**values)  # type: ignore[arg-type]


def test_copy_sequence_classification() -> None:
    item = policy()
    assert buttons.action_for_count(1, item, "copy") is None
    assert buttons.action_for_count(3, item, "copy") == "refresh"
    assert buttons.action_for_count(5, item, "copy") == "upgrade"
    assert buttons.action_for_count(8, item, "copy") == "reboot"
    assert buttons.action_for_count(12, item, "copy") == "poweroff"


def test_power_button_never_maps_package_actions() -> None:
    item = policy()
    assert buttons.action_for_count(5, item, "power") is None
    assert buttons.action_for_count(8, item, "power") == "reboot"


def test_every_destructive_action_defaults_disabled(tmp_path: Path) -> None:
    config = tmp_path / "buttons.conf"
    config.write_text("enabled=true\n")
    item = buttons.load_policy(str(config), "copy")
    for action in ("refresh", "upgrade", "reboot", "poweroff"):
        assert buttons.action_allowed(action, item) is False


def test_blocked_action_does_not_spawn_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(buttons.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not execute"))
    assert buttons.execute_action("poweroff", policy()) == 3


def test_explicit_opt_in_allows_only_selected_action() -> None:
    item = policy(reboot_enabled=True)
    assert buttons.action_allowed("reboot", item) is True
    assert buttons.action_allowed("poweroff", item) is False


def test_allowed_action_uses_injected_harmless_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(buttons, "ACTION_LOCK", str(tmp_path / "action.lock"))

    result = buttons.execute_action(
        "reboot",
        policy(reboot_enabled=True),
        runner=lambda command: calls.append(command) or 0,
        identity_check=lambda: True,
    )

    assert result == 0
    assert calls == [["systemctl", "reboot"]]


def test_allowed_action_refuses_unsupported_hardware() -> None:
    calls: list[list[str]] = []
    result = buttons.execute_action(
        "poweroff",
        policy(poweroff_enabled=True),
        runner=lambda command: calls.append(command) or 0,
        identity_check=lambda: False,
    )
    assert result == 4
    assert calls == []


def test_input_aliases_are_deduplicated(tmp_path: Path) -> None:
    device = tmp_path / "event0"
    device.write_bytes(b"")
    alias_a = tmp_path / "alias-a"
    alias_b = tmp_path / "alias-b"
    alias_a.symlink_to(device)
    alias_b.symlink_to(device)

    assert buttons.deduplicate_input_paths((str(alias_a), str(alias_b))) == (str(alias_a),)
