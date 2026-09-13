# Safety

Proxnap writes fan PWM and chassis indicators as root. A bad mapping or an
unexpected driver response can overheat the machine. Keep firmware and Proxmox
thermal shutdown protection active, and watch every temperature and tachometer
during the first deployment.

Every hardware command requires an accepted 12-bay DMI identity (`TVS-1288X` or
`TVS-H1288X`) or the qnap8528 VPD pair `Q05W0` and `Q05K0`. DMI acceptance is
not a claim that every hardware revision has been validated, and the runtime has
no bypass for the TVS-h1688X.

## Fan fallback

Fan-zone names are fixed in the code. Configuration cannot redirect a fan write
to an arbitrary sysfs path.

The controller requests `pwm1=255` and `pwm7=255` after:

1. SIGTERM, SIGINT, or a normal daemon stop;
2. an unhandled loop or configuration error;
3. a failed PWM write or readback check;
4. loss of a configured temperature source;
5. an expected tachometer that is missing, unreadable, or stuck at zero for the
   configured grace period.

If the qnap8528 hwmon interface disappears, software cannot command full speed.
The process exits with an error, records a visible fault when it can, and lets
systemd retry. Firmware protection is still necessary.

Do not lower a fan floor until every fan has survived cold starts and sustained
load at that setting. Check that both curves reach full duty at their critical
temperature.

## Writer locks

Fan, panel, status, and disk LED writes share
`/run/lock/proxnap-ec.lock`. LCD writers use
`/run/lock/proxnap-lcd.lock`. The locks reduce conflicting writes between Proxnap
services, but they do not change the driver's behavior and cannot coordinate
unrelated tools.

Static panel brightness is the default. Breathing mode changes the global level
repeatedly and can produce visible or audible controller side effects. The two
systemd units conflict so they cannot run together.

## Buttons

Both button daemons ship with `enabled=false`. Package refresh, distribution
upgrade, reboot, and poweroff also ship disabled. Turning on a listener does not
grant any action.

An allowed action still needs an arm sequence, a separate confirmation sequence
inside a short window, and the global action lock. The handler chooses from a
fixed command list; configuration cannot supply a shell command.

Package work can still leave the host unbootable. Use a console, a verified
backup, and a maintenance window, and review package changes manually before
enabling that path.

When the custom power listener is enabled, the installer tells systemd-logind to
ignore its own power-key action. Removal deletes that override.

## Recovery

If the fans do not behave as expected, stop optional LED and button services and
request full speed directly:

```bash
sudo env PYTHONPATH=/opt/proxnap /usr/bin/python3 -m proxnap.fan_control --failsafe
```

If readback does not return `255` for both zones, shut the machine down through a
controlled external method. Do not reload the controller until the driver or
hardware fault has been understood.
