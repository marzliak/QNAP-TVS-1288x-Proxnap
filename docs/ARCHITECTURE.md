# Architecture

## One portable implementation

Reusable code lives only in `src/proxnap/`. Installed commands and console scripts
use `proxnap.runtime`, which checks the supported chassis before importing a
hardware-facing implementation. The standalone PWM module is intentionally
byte-preserved and internal; use the guarded entrypoint, not a direct module
invocation. `--failsafe` remains an alias for `--safe` on that entrypoint.

| Component | Responsibility | Default installation |
|---|---|---|
| `fan_control` | validated config, sensor inventory/cache, immediate thermal rise, controlled descent, PWM readback and two-zone protection | enabled |
| `lcd` | landing frames, section menus, serial button parser, snapshot freshness and shared display ownership | enabled |
| `status_led` | tri-state temperature/SMART/service/update health and status JSON | enabled |
| `panel_led` | static brightness; optional breathing and off-hours policy | static |
| `copy_update` | paired Copy events, fresh confirmation and guarded update/power backend | listener and actions disabled |
| `power_button` | independent-source pairing, reconnect/SYN recovery and confirmation | listener and actions disabled |
| `disk_leds` | explicit bay mapping, activity, SMART critical/unknown distinction | disabled |
| `metrics` | optional InfluxDB line protocol, explicit status freshness, sensor availability and collection tiers | disabled |
| `blue_temp_breath` | optional temperature pulse on the USB LED | manual only; conflicts with status/Copy indication |
| `fan_diagnostics` | explicit per-tachometer stall diagnostics | not polled by the PWM loop |
| `acpi_fans` | legacy ACPI cooling-state helpers, not PWM | experimental helpers only; no service or automatic action |

`buttons` retains the published policy/helper API and dispatches listeners to the
single Copy or Power state machine. There is no second legacy daemon tree.
The former combined ACPI/LCD daemon is superseded by `lcd`; only its distinct
ACPI helper remains. Host CPU governors, PCI/NVMe recovery and raw EC experiments
are not library features.

## Files and commands

- Package: `/opt/proxnap/proxnap`; configuration: `/etc/proxnap`.
- Units: `/etc/systemd/system`; thin compatibility commands: `/usr/local/sbin/proxnap-*`.
- `proxnap-fan-control`, `proxnap-lcd`, `proxnap-copy-update`, `proxnap-power-button`,
  `proxnap-status-led`, `proxnap-panel-led`, `proxnap-disk-leds`, `proxnap-metrics`.
- Logs: journal and optional configured button log files; transient state: `/run`.
- No third-party Python runtime dependencies.

## Locks and their limits

The PWM controller holds `/run/proxnap-fan-control.lock` for its complete actuation
lifetime, including protection writes. An unavailable lock does **not** authorize
unprotected fallback writes. PWM readback is quantized driver state, not proof of
EC ownership, fan response or an autonomous manual-mode lease.

All LCD writers use `/run/proxnap-lcd-message.lock`. Ownership spans initialization,
frames, every write delay, messages and cleanup. A competing owner suspends hidden
UI actions, and fresh input/context are required after reacquisition.
Copy, Power and LCD-dispatched power actions share `/run/lock/proxnap-action.lock`.

There is no claimed userspace global lock around all LED and PWM traffic.
Advisory locks coordinate only their cooperating writers; they neither repair
kernel transaction semantics nor coordinate third-party tools.

## Trust boundary

The driver and Linux hwmon/input/TTY interfaces are external dependencies. Root
configuration is trusted: update/buzzer command hooks are configurable, run without
a shell by the update runner, and are not a security boundary against a malicious
administrator. Package and power permissions remain separate explicit opt-ins.
Metrics writes require locally supplied endpoint and credentials; no network
endpoint, machine identity table or token is shipped. Measurement names use the
`proxnap_` prefix and serial-to-slot labels are optional local configuration.
