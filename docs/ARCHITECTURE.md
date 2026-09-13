# Architecture

## Installed files

- Python package: `/opt/proxnap/proxnap`
- Configuration: `/etc/proxnap`
- systemd units: `/etc/systemd/system`
- Fault and lock state: `/run` and `/run/lock`
- Logs: systemd journal

The Python package has no third-party runtime dependencies.

## Services

| Component | Reads | Writes | Default |
|---|---|---|---|
| `fan_control` | hwmon temperatures and tachometers | `pwm1`, `pwm7`, fault state | enabled |
| `lcd` | temperatures, memory, RPM, LCD buttons | 16x2 display | enabled |
| `status_led` | fan fault, module, temperatures, failed units | status LED | enabled |
| `panel_led` | fixed level or sine-wave level | global panel brightness | static |
| `disk_leds` | block counters and SMART results | per-bay LEDs | disabled |
| `buttons --kind copy` | qnap8528 input events | fixed, guarded actions | disabled |
| `buttons --kind power` | ACPI power events | guarded reboot or poweroff | disabled |

The LCD shows temperature, memory use, fan range, date, and time. Proxnap has no
network client or telemetry exporter.

## Locks

Every qnap8528 EC writer uses the same advisory `flock`. LCD output has a
separate lock. Button actions use a third lock so update, reboot, and poweroff
cannot start at the same time through the physical controls.

These locks coordinate Proxnap processes only. They cannot make the embedded
controller transactional, and they do not protect against another program that
writes the same sysfs nodes without taking the lock.

## Trust boundary

The qnap8528 driver and the Linux hwmon, input, LED, and TTY interfaces are
outside this repository. Install the module separately. Configuration is trusted
as root input. It cannot supply a shell command, and the accepted PWM targets
and button actions are fixed in code, but it can point the LCD and button
daemons at different device nodes.
