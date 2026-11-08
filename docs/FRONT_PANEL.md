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

## Information-only Power status popup (opt-in)

The existing LCD daemon can independently read a short `KEY_POWER` press and
explain the same `/run/proxnap-status.json` snapshot used by the status indicator.
This is **not** the legacy power-action listener. Leave that listener disabled;
no new service, socket, network connection or action API is needed. A Power press
is never an LCD Enter/Next gesture, never acknowledges/clears an alert, and cannot
change fan mode, refresh packages, install updates, reboot or shut down. Status
red/green and USB Copy blue indications retain their existing owners/policies.

The shipped `lcd.conf` keeps this feature off:

```ini
power_status_enabled=false
power_status_inputs=
power_status_page_seconds=3.0
```

To opt in, supply an explicit CSV of locally verified absolute evdev paths (stable
`/dev/input/by-path/` aliases are supported) and set `power_status_enabled=true`.
Do not put these paths in `button_input`, and do not use the qnap8528 Copy/chassis
input as a Power source. No device discovery is performed. Up to eight distinct
configured paths are retained; relative paths are ignored. The resolver opens the
resolved event node read-only, nonblocking, close-on-exec and no-follow, then checks
the actual FD is an input/event character device with matching sysfs device
identity, `Power Button` name and `KEY_POWER` capability. Duplicate aliases never
retain two FDs for the same device. There is no exclusive grab or writing ioctl.
Unplug/EOF/errors close the FD and retry after two seconds. Reloading configuration
preserves unchanged FDs and closes removed/disabled inputs.

By default (`status_scroll_enabled=false`), a fresh press wakes the backlight and
shows `Critico` or `Aviso` with numbered 16-column reason pages. Fixed labels are
Portuguese ASCII; producer reason text
is ASCII-normalized and wrapped, not silently cut to the first 16 characters.
Critical reasons take precedence over unavailable sources. Warnings use the
`warnings` list, not critical reasons. `Tudo OK` requires a coherent fresh `ok`
snapshot with empty reason/warning/unavailable lists. Missing, malformed,
incoherent, stale, future or nonfinite state yields `Sem dados / Desconhecido`.
Freshness uses the existing critical LCD page's rule: integer positive timestamp,
finite positive `check_seconds` (default 30), and age from zero through that period.

Each page lasts `power_status_page_seconds`: finite 1–10 seconds, default/fallback
3.0. A snapshot read is bounded to 65,536 characters and a popup to 32 pages
(at most 320 seconds with the maximum page timing). Normal reasons are complete;
excessive reason text ends with `Mais: limite LCD`. Another Power press during a
popup is consumed without restarting/extending it. This is a point-in-time
snapshot, not a live alarm-acknowledgement screen.

Safe home/cover/detail navigation and the pinned home frame are retained; menu
inactivity is paused. Pending administrative confirmations, update/fan submenus
and temporary overrides are cancelled, returning to home rather than rearming
an action. LCD buttons are drained during the popup, with no hidden UI tick or
button processing. Press state is reset at both boundaries; a held button must
be released before navigation is accepted again. After the last page the safe
panel is redrawn; normal inactivity/backlight policy resumes.

All serial output remains in the LCD loop under the existing shared transaction
lock. External Copy/action messages have ownership priority: Power events are
drained/discarded while ownership is denied and on reacquisition, an interrupted
popup is cancelled, and no pending request is replayed. Event timestamps must use
evdev's default realtime clock, be no older than one second, not future-dated and
not predate the current open/suspension boundary. Press/release can be instantaneous
(no Copy-style minimum hold); autorepeat, bounce and duplicate ACPI sources within
350 ms do not create extra requests. No OS/logind/ACPI power policy is changed by
this feature: the operator must separately ensure the platform's Power policy is
appropriate before opting in.

The offline suite AST-isolates the actual reader, popup and bounded daemon loop
with mandatory fake serial/input/metadata/worker/effect providers and scratch-only
flock. Those tests are not physical validation of a particular input alias or LCD.

## Isolated Copy click status popup (opt-in)

The existing Copy listener can request the same information-only popup after
**exactly one valid released short click**, once the existing
`click_sequence_settle_seconds` quiet period has elapsed (default two seconds).
There is no popup for two/three clicks, maintenance/confirmation sequences, holds,
autorepeat, bounce, malformed/stale events or disconnected/partial gestures. A
pending Copy maintenance confirmation never becomes a status request. Every new
valid Copy press sends cancel immediately, so a later hold or multi-click sequence
is not hidden by an earlier popup. No maintenance threshold or action is changed.

Both ends ship disabled. To opt in, enable the existing Copy listener with its
locally verified input binding and configure matching names in both programs:

```ini
# copy-button.conf (the Copy listener's actual --config file)
single_click_status_enabled=true
status_socket=@proxnap-lcd-status

# lcd.conf
copy_status_enabled=true
copy_status_socket=@proxnap-lcd-status
```

The LCD does not read or classify Copy events. The only new transport is a Linux
abstract `AF_UNIX/SOCK_DGRAM` endpoint in the same network namespace; `@` denotes
the leading NUL, **not a filesystem socket path**. Names are bounded ASCII, up to
106 characters after `@`. There is no new service, input reader, persistent
mailbox, command payload or public/network listener. The fixed 13-byte protocol
is `PLS1`, one byte `S` (status) or `C` (cancel), then an unsigned big-endian
64-bit monotonic nanosecond timestamp. `SO_PASSCRED`/`recvmsg` require exactly one
kernel credential record with UID0. Unknown, truncated, unauthenticated, future,
pre-bind, over-two-second-old and replayed timestamps are rejected. At most 16
datagrams are drained per iteration; exhausting that budget discards any status
result rather than displaying an uncertain request and invalidates the remaining
captured backlog. An authenticated Copy cancel dismisses any informational
popup, including one requested by Power: maintenance feedback takes priority.

Sending is nonblocking/best effort, never a command or external LCD-write fallback.
A missing/busy receiver cannot abort maintenance; one bounded result is logged per
settled single-click request (`sent` means queued locally, not physically visible).
Bind failures leave normal LCD rendering working and retry no faster than every
five seconds; operators must verify socket readiness before enabling the shortcut.
Disable/re-enable, receive errors and endpoint changes close/reset the receiver;
the prior name is released and old requests cannot trigger after rebinding.

The existing status snapshot parser and popup are shared with Power status, not
copied into the Copy listener. Critical reasons survive partial unavailability;
missing/invalid status remains explicitly unknown. Each page uses the existing
`power_status_page_seconds` setting (default three seconds). A request wakes a dark
LCD, preserves safe navigation and a fixed home frame, then returns automatically.
Cancel also restores safely, without replaying held navigation or administrative
confirmations. Requests arriving without LCD ownership or during reacquisition
are discarded, never deferred; an external writer interrupts the popup permanently.
Repeated status requests during a popup do not extend it. Existing Power opt-in
and navigation behavior remain independent.

Offline tests exercise unique abstract sockets and the actual AST-isolated Copy
and LCD loops with inert hardware/final-action providers. They do **not** prove the
physical Copy binding, a visible message, automatic return on real hardware, or
deployment of this package. Physical acceptance remains a separate operator gate.

## Readable automatic status scrolling (opt-in)

The same popup can show a fixed severity/cause header and automatically scroll
only the second line. This is presentation in the existing LCD owner, not a new
service, input reader, library, serial protocol or acknowledgement mechanism.
The legacy pager above remains the exact default. Enabling scrolling alone does
not enable either Copy or Power status and does not authorize maintenance.

```ini
# lcd.conf: shipped values; explicitly set true to opt in
status_scroll_enabled=false
status_scroll_step_seconds=0.6
status_scroll_pause_seconds=1.5
status_scroll_max_seconds=60
# Optional example only, replaced by a locally selected exact unit ID:
# status_label.example-prepare.service=Falha ao preparar o driver
```

For example, `CRITICO 1/2` remains fixed while a human-readable Portuguese cause
slides through a 16-column window. The index counts causes, not wrapped fragments;
each valid unit in `failed systemd units: ...` becomes a separate cause. The first
window stays for 1.5 seconds; the first shift happens then, followed by one
character every 0.6 seconds. The final full window stays for 1.5 seconds. A short
cause stays still for both pauses (three seconds by default). Causes advance
once automatically, without another click. After the last cause, the popup returns
to safe HOME, including the operator's fixed home frame; pending actions and held
navigation never resume. Repeated status requests do not restart/extend the run.

Timings must be finite. Invalid/nonfinite values use the displayed defaults;
finite values clamp to 0.6–2 seconds per step (no faster UART traffic), 1–5 seconds
per pause, and 10–60 seconds total. The nonblocking monotonic timeline expires
within that total budget; normal loop scheduling/UART latency still applies.
Only changed scrolling frames are sent through the existing serial write path;
there is no per-step sleep or firmware scrolling command. Legacy-page rendering
and normal navigation retain their existing refresh behavior.

At most 32 timeline entries and 512 characters per cause are presented, subject
to the total budget. A cause may be cut short and later causes omitted. In every
such case, the count gains `+` and a final `Mais...` notice is reserved (one of the
32 entries), with both pauses; even a budget that fits only the notice remains
explicit. `1/32+` is a bounded count, **not** a claim that all causes were shown.
Use the unchanged full producer snapshot/journal for technical detail. The
65,536-character snapshot input bound and original freshness/type/coherence
validation remain shared with the legacy pager, not duplicated.

Known producer phrases are translated without inventing a diagnosis. A fresh
unknown snapshot with `configured disk unavailable: bay-a` shows `SEM DADOS`
and scrolls `Disco configurado indisponivel: bay-a`; this does not say the disk is
defective or healthy. Known critical reasons still take precedence over missing
sources. Other invalid/stale/incoherent data shows `SEM DADOS` with
`Dados indisponiveis`; a coherent fresh healthy snapshot alone shows `Tudo OK`.
Unrecognized source text is ASCII-normalized faithfully, not reinterpreted.

`status_label.<EXACT_SYSTEMD_UNIT_ID>=human label` is an optional private alias.
The loaded `status_labels` dictionary uses the literal full unit ID as its key,
including suffix/instance/escaping. No case folding, prefix matching, suffix
completion or whitespace repair is applied; write the key without spaces before
`=`. Invalid identifiers, blank/over-512-character labels and control characters
are ignored. Missing aliases fall back to `Servico em falha: <id>`. An alias
changes only presentation, not the snapshot/identifier or status. Neither a unit
name nor a collector timestamp establishes that a failure is old, recovered or
resolved; labels must not assert those conclusions without independent evidence.
Public examples deliberately contain no deployment unit IDs or labels.

External writers interrupt and permanently cancel the popup; denied/reacquiring
ownership drains/discards requests instead of replaying them. Disabling the LCD
or the originating shortcut, or rebinding that shortcut, cancels its popup. A Copy
cancel from a fresh press dismisses any informational popup before maintenance;
unrelated configuration of the other shortcut does not dismiss a popup. Buttons
are drained without hidden tick/actions; both boundaries require release before
any fresh navigation. All Copy gestures,
maintenance/energy dispatch, status production, IPC and hardware paths are unchanged.
Offline clock/AST/socket/loop tests exercise these contracts with inert effects;
readability and automatic return on the actual panel require physical acceptance.

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

For existing installations, `lcd.conf` can set `copy_update_config` to the actual
Copy listener configuration path. It defaults to `/etc/proxnap/copy-button.conf`
and affects **only** Ref+Run, Refresh and Run update callbacks. Reboot/Shutdown
callbacks retain their existing constant/default path; this setting must not
grant them permissions from a different Copy config. The current backend has
fail-closed package/power defaults, unlike some legacy installations. Before
adoption, preserve deliberately authorized package settings in the actual config
and check update-menu path compatibility. Do not fix or enable a previously
nonworking energy command chain as a side effect of this informational shortcut.

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
