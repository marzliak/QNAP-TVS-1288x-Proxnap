# Front-panel controls

## LCD

The 16x2 display uses `/dev/ttyS1`, 1200 baud and the QNAP/ICP serial protocol.
Text is ASCII-fitted to 16 columns. Serial input preserves partial frames across
reads, retains at most three bytes, and clears partial state on connection errors
or reopen.

The landing display rotates through pressure (load/RAM), workload
(hostname/uptime/VM/LXC counts), and aggregate network throughput. A missing,
failed or expired live snapshot shows `unknown`, not fabricated zero measurements.

- Lower button (`0x02`): advance the landing frame or next section/detail.
- Upper button (`0x01`): enter the numbered menu, enter a section, return to its
  cover, or select an action.
- Upper hold on HOME: lock/unlock the current landing frame.
- A dark display consumes the wake gesture; it does not execute a hidden action.

Sections are Status, Network, Storage, Thermal and Actions. They include local
services/alerts, visible interface statistics, pools, temperature and fan controls.
Home frames rotate every seven seconds, detail pages every eight seconds, and
backlight inactivity defaults to five minutes. The config controls those timings.
Missing thermal values are shown as `?`, not zero degrees.

The shared LCD message lock covers complete transactions, not only individual
writes. Copy/Power feedback owns the display through close and any configured
final delay. Navigation/held confirmations cannot continue invisibly through an
external message. The daemon refreshes visible context after ownership changes.

## Status and lighting

| Indication | Meaning |
|---|---|
| Solid green status | complete local health checks found no critical or warning condition |
| Red status/blink | known critical health condition |
| Green status with USB Copy pulse | warning/update/reboot-required condition |
| Unknown indication | required sources unavailable; not a healthy verdict |
| Off | explicit off/disabled monitoring; status JSON is unknown when monitoring is disabled |

Critical conditions include known high temperatures, failed systemd units,
unhealthy pools, critical SMART results, read-only root and missing driver.
Unknown temperature/SMART/package data never closes a known critical incident.
Status JSON carries source timestamp, check period and unavailability; stale,
future or incoherent payloads do not produce healthy LCD/metrics output.

Panel brightness is static by default. Optional breathing and off-hours policy
control the global panel PWM and can affect other lit indicators. Repeated EC
writes can have visible/acoustic side effects. The separate temperature-aware
USB LED pulse is manual-only and must not compete with status/Copy writers.

## Drive LEDs

Map physical bays explicitly to whole devices. Do not infer bay order from Linux
enumeration. The repository supplies no machine map. Healthy recent activity is
green, healthy idle is off, critical SMART is flashing red, and unavailable SMART
is **not** a green health assertion. Missing devices remain off. `--no-smart` is
explicit activity-only mode, not a health check.

## USB Copy and Power

Listeners and all final effects ship disabled. Enabling a listener alone cannot
run package or power commands. Fresh press/release pairs are required; duplicate,
repeated, stale or malformed events cannot count as extra clicks.

Copy defaults: a deliberate hold of two seconds can refresh metadata only with
the refresh opt-in. Four to seven clicks arm refresh+upgrade, eight to twelve arm
reboot, and thirteen or more arm poweroff. Each armed multi-click action needs
three **new** confirmation clicks after visible feedback. Combined refresh+upgrade
requires both package permissions; install-only requires the upgrade permission.
An update never reboots automatically.

Power defaults: five clicks arm reboot, ten arm poweroff, then three fresh
confirmation clicks. Input aliases are deduplicated; sources cannot combine
pairs or confirmations. EOF/reconnect, dropped events and `SYN_DROPPED` invalidate
pending state and require a new complete sequence.

The Actions menu dispatches update and power requests through the same guarded
Copy backend; it does not bypass the explicit power/package permissions. It uses
visible LCD confirmation, not a requirement to click the Copy button itself.

Existing first-release button configuration may use different click semantics;
review the current templates rather than assuming old numeric thresholds still
have identical meaning. `apt_refresh_enabled`/`apt_upgrade_enabled` remain accepted
aliases for `short_refresh_enabled`/`upgrade_enabled` when the new key is absent.
See [installation](INSTALL.md) and [safety](SAFETY.md).

## Buzzer

Audible patterns were physically checked under the original QNAP HAL, not through
this package. No validated native Linux buzzer implementation or raw EC command is
supplied. Optional root-supplied command hooks remain empty and disabled; neither
hardware capability nor those hooks establish current-package physical acceptance.
