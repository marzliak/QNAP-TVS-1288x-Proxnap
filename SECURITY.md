# Security policy

## Supported hardware

Report issues against a precise revision; integration work uses `homolog`. Hardware testing covers the
QNAP TVS-1288X (12-bay) only. The TVS-h1688X (16-bay) is neither tested nor
supported.

## Reporting a vulnerability

Use a private security advisory on GitHub. Before attaching logs, remove
credentials, network details, serial numbers, and anything else that identifies
the machine or its owner.

## Root access

The daemons run as root because Linux exposes the embedded controller, PWM, LEDs,
input devices, and LCD through privileged nodes. Treat both the installation
source and `/etc/proxnap` as root-trusted input.

Root-owned configuration can supply update command arguments and optional buzzer
hooks; it is trusted administrative input, not a sandbox. The update runner uses
argv rather than a shell. Package refresh, upgrade, reboot and poweroff remain
separate disabled-by-default permissions. Armed physical actions require fresh
confirmation; Copy hold-refresh is a distinct explicit opt-in gesture. LCD power
requests use the same guarded backend.

An optional metrics service can read local device identities and send telemetry
to an operator-configured InfluxDB endpoint. It is not enabled automatically, and
no endpoint, credential or machine identity map is shipped. Keep installed metrics
credentials mode 0600, review labels before export, and never commit real tokens or
serial-to-slot maps. The local chassis controls need no network connection.
