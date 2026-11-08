# Safety

Proxnap writes fan PWM and chassis indicators as root. Preserve firmware thermal
protection and an external console/recovery path. Offline tests do not validate
physical airflow, noise, controller timing or shutdown sequencing.

Installed hardware-facing commands use the TVS-1288X/TVS-H1288X DMI guard or, for
generic DMI only, the tested Q05W0/Q05K0 VPD pair. Explicit unsupported DMI wins
over conflicting VPD. The TVS-h1688X is refused. Internal modules are not alternate
unguarded installation interfaces; use the guarded installed/console entrypoints.

## Fan fallback

CPU is fixed to `pwm7`, rear to `pwm1`. Configuration cannot remap those targets.
Continuous floors are CPU **50%**, rear **40%**. Configured manual values, profiles
and descent smoothing cannot override those floors or suppress a raw-temperature
increase. The stop/protection request is temporarily **100%**, never a 25% policy.

The controller retains sensor inventory across reloads, rejects malformed,
non-finite, duplicate or unknown configuration, invalidates expired/reloaded sensor
results and does not silently adopt a disappearing source as healthy. Temperature
increases/emergencies act immediately; averaging, step and interval limits govern
controlled descent only. Failed writes/readback, missing required temperatures,
invalid config and orderly stop request full duty on both zones. Both zones are
attempted even when one write fails, under the same actuation lock.

Missing hwmon or a failed write can prevent actual cooling: a software request is
not a physical guarantee. Quantized readback proves only the driver-exposed value.
It does not prove ownership, tachometer response or an EC manual-mode lease.

Per-tachometer stall/missing diagnostics are retained in `fan_diagnostics`, but
**continuous RPM stall polling is not part of the default PWM loop**. Do not assume
a stalled fan triggers an automatic trip through that optional helper. Monitor
all five tachometers during target acceptance; no additional continuous EC polling
was enabled to conceal unresolved physical fan pulses.

## Writer locks

PWM: `/run/proxnap-fan-control.lock`; LCD messages: `/run/proxnap-lcd-message.lock`;
actions: `/run/lock/proxnap-action.lock`. LCD ownership includes initialization,
delays, frames and cleanup, and losing ownership cancels hidden gesture progress.
Lock failure must not cause unprotected writes. These are scoped advisory locks,
not a guarantee of kernel-level EC transaction atomicity or third-party exclusion.

Use static panel brightness unless repeated EC writes are justified. Optional
breathing, disk monitoring and USB LED effects add traffic. Do not run competing
writers for the USB/status indication. Compound driver locking remains an
experimental separate deliverable; the installer never applies driver fixes.

## Buttons

Listeners, refresh, upgrade, reboot and poweroff are disabled by default. Each
final-effect permission is checked in its backend, including LCD-dispatched power
requests. A separate confirmation sequence is required for armed multi-click
requests; capture-time barriers reject clicks queued during feedback. Copy hold
refresh is a distinct explicit opt-in gesture. Power pairs/confirmations cannot
span devices, drops or reconnect boundaries.

Root configuration can supply update command arguments and optional buzzer hooks;
it is trusted administrative input, not a sandbox. The update runner uses argv,
not shell evaluation. Package changes can leave a host unbootable. Review them,
keep a verified backup/console, and use a maintenance window. No package update,
reboot or poweroff has been executed by the offline test suite.

## Recovery

With console access and the correct driver present, use the guarded command:

```bash
sudo /usr/local/sbin/proxnap-fan-control --safe
# Historical spelling remains supported by the same launcher:
sudo /usr/local/sbin/proxnap-fan-control --failsafe
```

If full-duty readback cannot be verified on both zones, use a controlled external
shutdown and investigate the hardware/driver before unattended operation. Stopping
optional effects is not permission to stop cooling or lower thermal safeguards.
