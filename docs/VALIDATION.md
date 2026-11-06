# Validation

The reference TVS-1288X was running Proxmox VE 9.2.26 and kernel
`7.0.14-15-pve` on 6 November 2026, after 62 days of uptime. Its five ZFS
pools were healthy, and fan control, LCD, and status LED services were active.
The full dated readings are in [REFERENCE_SYSTEM.md](REFERENCE_SYSTEM.md#operating-record).

## What the evidence covers

Physical checks establish that the interfaces work on the reference chassis.
Automated tests check this package with mocked hardware and temporary files.
They do not exercise a real embedded controller, systemd shutdown sequence,
or sustained thermal load.

The controller installed on the reference machine predates this repository's
Python package. The operating record belongs to that installed configuration;
a full deployment and endurance test of this exact package on the chassis
remain to be done.

| Area | Reference hardware evidence | Public package evidence and limits |
|---|---|---|
| Proxmox and kernel | 9.2.26 / 7.0.14-15-pve running at the November check | No compatibility claim for a kernel not exercised on the reference machine |
| Standard I/O | onboard USB ports, installed PCIe/NVMe/SATA devices, and Ethernet interfaces checked during conversion | Supplied by Linux drivers; Proxnap does not implement these drivers |
| Fans and sensors | both PWM zones, five tachometers, and thermal inputs observed; operational fan service active | Tests cover curve interpolation, fixed mapping, PWM scale, missing sensors, and individual missing/stalled fans |
| Two-zone fail-safe | the interfaces for both fan zones exist on the chassis | Tests verify full-duty writes and readback through simulated files; physical failure injection and stop-path acceptance for this package remain untested |
| LCD and its buttons | display, backlight, and button input physically mapped; operational LCD service active | Automated test covers 16-byte text formatting; the complete public daemon still needs chassis acceptance |
| Status and panel LEDs | status and panel lighting physically mapped; status and static-panel services active | Brightness bounds are tested; physical fault indication and breathing behavior need acceptance with this package |
| Drive LEDs | activity/color paths physically mapped; monitor inactive at the November check | Implementation requires an explicit bay map; no long-duration public-package hardware test |
| USB Copy and power input | Linux input paths physically mapped | Tests cover sequence classification, blocked defaults, identity checks, input aliases, and an injected harmless action runner; no real package update, reboot, or poweroff is tested |
| Buzzer | audibly checked with the original QNAP HAL | Not implemented by Proxnap |
| TVS-h1688X | not tested | Refused by the identity guard, including conflicting DMI/VPD cases covered by tests |

## Differences that matter during installation

The operational controller uses temperature averaging, PWM step/rate limits,
and a rear-fan policy based on HDD temperatures. This package uses the shipped
`storage` policy and its own hysteresis. It also adds per-fan stall detection,
PWM readback checks, and a two-zone full-speed fallback. Its stop fallback
requests full duty; an older controller's stop policy must not be assumed to
match it.

Do not copy a complete configuration from another controller and assume that
all keys or safeguards have the same meaning. Read the shipped config and the
[safety model](SAFETY.md), then follow the
[installation checks](INSTALL.md#hardware-check). Confirm each fan, sensor,
indicator, and input on the target chassis before unattended use.

An active status LED service proves the process is running, not that the LED
is green or that every host service is healthy. Likewise, a zero systemd
restart counter does not prove an incident-free history.
