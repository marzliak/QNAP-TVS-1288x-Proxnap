# Security policy

## Supported hardware

Security fixes target the current `main` branch. Hardware testing covers the
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

Button configuration cannot contain arbitrary shell commands. The daemon uses a
fixed action list, and package refresh, distribution upgrade, reboot, and
poweroff all start disabled. Each requires an explicit configuration change and
a deliberate physical confirmation sequence.

Proxnap does not send telemetry or read disk and chassis serial numbers.
