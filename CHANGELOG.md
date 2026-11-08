# Changelog

## Unreleased — status source availability

- A failed systemd query is unavailable, not evidence that no units failed. Status, LCD and metrics no longer publish healthy data after an empty/nonzero query result; recovery and unrelated known-critical conditions remain explicit.


## 2026-11-07

- Consolidate the portable PWM, LCD, Copy/Power, health, telemetry and LED
  implementations into the existing package layout, with thin compatible CLIs.
- Preserve the reference PWM source bytes, CPU50%/rear40% floors, 100% temporary
  stop/protection, strict sensor/config handling and immediate thermal response.
- Bring fragmented input, fresh confirmation, reconnect/SYN, shared LCD ownership
  and unknown/stale-data regressions into the public offline suite.
- Keep package/power actions and optional hardware writers disabled, including
  power requests from the LCD. Retain per-tachometer and legacy ACPI helpers
  explicitly without enabling additional continuous EC polling.
- Add exact payload staging under runtime scratch and optional telemetry with
  operator-supplied credentials/mapping. No target installation or physical test
  is implied by this source update.

## 2026-11-06

- Refresh the reference snapshot for Proxmox VE 9.2.26 and running kernel
  7.0.14-15-pve, with 62 days of uptime and five healthy ZFS pools.
- Update ZFS dataset capacity, fan RPM, temperature, memory, and CPU power-policy
  readings; date the older Proxmox storage inventory separately.
- Add a validation matrix separating physical checks, automated tests, and
  deployment of the public package.
- Add read-only troubleshooting for fan pulses, PWM/RPM interpretation, sensor
  alarms, and CPU power-policy checks.
- Keep runtime code, safety defaults, supported models, and installer behavior
  unchanged.
