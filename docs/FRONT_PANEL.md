# Front-panel controls

Proxmox can run the TVS-1288X well, but it does not know how to drive the
QNAP-specific LCD, LEDs, fans, or USB Copy input. Proxnap restores those parts
as local Linux services so the chassis remains an operating interface rather
than a row of inactive controls.

## LCD

The built-in 16x2 display is connected to `/dev/ttyS1` and uses the QNAP/ICP
protocol at 1200 baud. The default service rotates through three pages:

| Page | First line | Second line |
|---|---|---|
| System | `Proxmox VE` | local time and date |
| Temperature | hottest CPU reading | hottest system/storage reading |
| Resources | host memory use | observed fan RPM range |

Text is fitted to the physical 16-character width. Unsupported characters are
replaced rather than sent as arbitrary bytes to the controller.

The two buttons beside the LCD are live:

- the upper/confirm button reports mask `0x01`;
- the lower/next button reports mask `0x02`;
- either button wakes a dark display and advances the rotation immediately.

Pages advance automatically every six seconds by default. After five minutes
without a button press, the backlight turns off; the next press wakes it. Both
intervals are configurable in `/etc/proxnap/lcd.conf`.

The LCD has its own advisory lock. This prevents two Proxnap processes from
writing overlapping frames, but programs outside Proxnap must use the same lock
if they also write `/dev/ttyS1`.

## Status and panel lighting

The status LED is a compact local health indicator:

| Indication | Meaning |
|---|---|
| Solid green | no critical condition detected |
| Red | fan fault, missing `qnap8528`, critical temperature, or failed systemd unit |
| Off | explicitly requested for service or maintenance work |

A fan fault requests both fan zones at full duty and the red alert as part of
the same fail-safe response. The alert blinks off for half a second once per
check cycle, so it reads as steady red rather than a fast flash. Cooling comes
first; the light only reports it.

The blue front-panel lighting supports three modes:

- fixed brightness, enabled by default;
- a configurable breathing effect using a smooth sine-wave transition;
- off.

Breathing is opt-in because it writes the embedded controller repeatedly and
also changes the apparent brightness of other illuminated front-panel elements.

## Drive-bay LEDs

After the administrator maps each physical bay to its whole-device Linux name,
Proxnap can make the original drive LEDs useful again:

- green while I/O activity is observed;
- off while the mapped device is idle;
- flashing red when the SMART health check reports a critical result.

Mappings are deliberately empty in the repository. Linux device enumeration can
change with controllers and cabling, so enabling guessed mappings would make a
convincing but incorrect front panel. The installer refuses to enable the disk
LED service until at least one valid mapping exists. See
[disk LEDs](INSTALL.md#disk-leds) for the mapping and enable steps.

## USB Copy and power buttons

The USB Copy and power buttons are exposed as Linux input events. Proxnap can
translate deliberate click sequences into a small fixed action set:

- refresh package metadata;
- run a distribution upgrade;
- reboot;
- power off.

These listeners and every action are disabled by default. When an action is
enabled, its first click sequence only arms it; a second confirmation sequence
must arrive within a short window. Unknown sequences do nothing, paths resolving
to the same input device are deduplicated, and an action lock prevents two
physical actions from starting together.

The default sequence thresholds are documented in the shipped configuration
files. They can be changed, but destructive actions should remain explicit
opt-ins. The [safety model](SAFETY.md#buttons) covers the guard chain, and
[INSTALL.md](INSTALL.md#usb-copy-button) covers the opt-in flags.

## Buzzer

The chassis buzzer itself is functional. On the reference unit, short, long, and
repeated/locate patterns were audibly verified through the original QNAP HAL.
This is a simple buzzer rather than a voice-alert speaker.

That hardware test does not make buzzer control a Proxnap feature. The QNAP HAL
commands are not available under Proxmox, and Proxnap has no native Linux or
embedded-controller path to the buzzer. It emits no beeps at all and signals
nothing through sound.

## What starts after a default install

| Function | Default |
|---|---|
| Two-zone fan control | enabled |
| LCD pages and LCD buttons | enabled |
| Health status LED | enabled |
| Fixed panel brightness | enabled |
| Breathing panel effect | disabled |
| Drive-bay LED policy | disabled until mapped |
| USB Copy action listener | disabled |
| Power-button action listener | disabled |
| Package upgrade, reboot, and poweroff actions | disabled |

All hardware-facing commands first enforce the TVS-1288X identity check. The
untested TVS-h1688X is refused rather than treated as a compatible larger
chassis. [INSTALL.md](INSTALL.md#identity-check) gives the DMI and VPD rules.
