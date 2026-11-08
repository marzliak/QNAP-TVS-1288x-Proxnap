# Changelog

## Unreleased — status availability and configurable metrics

- Add opt-in readable LCD status scrolling: fixed Portuguese severity/cause
  header, one-character 16-column body windows, initial/final pauses and automatic
  safe-HOME return with fixed-frame preservation. The legacy pager remains the
  default; finite clamped timings, bounded causes/text and a total 60-second cap
  reserve an explicit `Mais...` notice for any omitted content.
  Exact private `status_label.<unit ID>` aliases affect presentation only;
  unavailable configured disks remain `SEM DADOS`, not failed or healthy disks.
  Reuse snapshot validation, UART and IPC; preserve ownership, fresh navigation
  and all maintenance/energy guards. AST/fake-clock/socket tests are offline,
  not deployment or physical scrolling acceptance.
- Add opt-in isolated short Copy click status through a local Linux abstract
  datagram socket between the existing input and LCD owners. Exactly one released
  click waits for the existing settle; every new valid press cancels the popup.
  UID0 credentials, monotonic freshness/replay guards and bounded draining reject
  untrusted or queued requests; external LCD writers always take priority.
  Reuse the existing paginated status snapshot and safe automatic return. Existing
  maintenance gestures, thresholds and Power input behavior are unchanged.
- Add LCD `copy_update_config` for the three update callbacks only, allowing an
  existing nondefault Copy config path without widening LCD energy authorization.
  All new status defaults remain off. Offline source/socket/loop tests do not
  establish deployment, physical Copy input mapping, or visible LCD acceptance.
- Add an opt-in, information-only short `KEY_POWER` popup to the existing LCD
  loop, reading the status indicator JSON without clearing alerts or dispatching
  actions. Explicit validated read-only evdev aliases, reconnect/backoff,
  timestamp/deduplication guards, paginated reasons and shared LCD ownership keep
  this separate from the disabled legacy Power listener. Offline AST/loop tests
  cover hidden-action rejection, safe navigation restoration and config reload;
  no service/deployment or physical validation is implied.
- A failed systemd query is unavailable, not evidence that no units failed. Status, LCD and metrics no longer publish healthy data after an empty/nonzero query result; recovery and unrelated known-critical conditions remain explicit.
- Add `PROXNAP_METRICS_MEASUREMENT_PREFIX` (default `proxnap`) to preserve an
  operator-selected existing measurement namespace in HTTP and dry-run output.
  Only the initial measurement prefix changes; suffixes, tags, fields, escaping
  and timestamps remain intact.
- Add validated `PROXNAP_METRICS_SERVICES` CSV configuration, retaining the exact
  generic default inventory and publishing inactive/disabled units as data.
  InfluxDB credentials/destination, sampling cadence and slot mapping are unchanged.
- Add offline compatibility, injection-rejection and output-parity regressions.
  No installation, service action or physical validation is implied.

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
