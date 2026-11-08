# Installation

The file payload is for a verified supported chassis with inactive application services. It refuses active, failed or transitional controllers rather than overwriting live code. A live upgrade or shutdown of an existing fan controller needs a separate thermal-protection and recovery procedure; this installer does not invent one. The direct payload entrypoint enforces the same chassis/root boundary. Uninstall keeps code and units if the final ventilation request fails.


Proxnap supports the QNAP TVS-1288X on Proxmox VE 9.x. Install a compatible
`qnap8528` module first. The TVS-h1688X has not been tested and is refused by the
model check.

Read [HARDWARE_SUPPORT.md](HARDWARE_SUPPORT.md) and [SAFETY.md](SAFETY.md) before
the first hardware run.

## Install the qnap8528 driver

Proxnap depends on the external
[`0xGiddi/qnap8528`](https://github.com/0xGiddi/qnap8528) kernel module. The
reference machine uses commit
`b75d5251b44a8a4b15b4e0846404f255a96fdbfa`; do not substitute a newer revision
without repeating the hardware checks.

Install the compiler, Make, DKMS, Git, and headers for the running Proxmox
kernel, then build that exact revision:

```bash
sudo apt-get install git make gcc dkms "proxmox-headers-$(uname -r)"
git clone https://github.com/0xGiddi/qnap8528.git
cd qnap8528
git checkout b75d5251b44a8a4b15b4e0846404f255a96fdbfa
sudo make install
dkms status
sudo modprobe qnap8528 preserve_leds=true
```

Confirm that `dkms status` lists `qnap8528` for the running kernel and that
`modprobe` returns without error. The Proxnap installer adds the persistent
module-loading unit; these commands only install and test the prerequisite.
After a Proxmox kernel update, confirm that DKMS built the module for the new
kernel before rebooting unattended.

## Preflight

```bash
bash scripts/install.sh --preflight
bash scripts/install.sh --dry-run
```

Preflight checks the model, Python, systemd tools, qnap8528 interface, and
`/dev/ttyS1`. Dry-run reports the same failures and prints the planned work
without writing files.

Some TVS-1288X units return a generic DMI product name. On those machines, load
qnap8528 before preflight so the installer can read the `Q05W0` mainboard and
`Q05K0` backplane codes from VPD sysfs. There is no switch to bypass the model
check.

## Default install

```bash
sudo bash scripts/install.sh
```

The default enables module loading, fan control, LCD output, the status LED, and
static panel brightness. It leaves breathing mode, disk LEDs, both button
listeners, package actions, reboot, and poweroff disabled.

The installer keeps configuration files already present in `/etc/proxnap`, so a
local fan policy survives an upgrade. The button opt-ins are the exception:
every run rewrites the `enabled`, action, reboot, and poweroff keys in
`copy-button.conf` and `power-button.conf` from the flags on that command line.
They do not accumulate across runs, so repeat every flag you still want.

## Hardware check

```bash
systemctl --no-pager status proxnap-fan-control proxnap-lcd proxnap-status-led
journalctl -u proxnap-fan-control -n 50 --no-pager
for item in /sys/class/hwmon/hwmon*/pwm1 /sys/class/hwmon/hwmon*/pwm7; do
  test -r "$item" && printf '%s=' "$item" && tr -d '\n' < "$item" && printf '\n'
done
```

Confirm that each fan starts, keeps a nonzero RPM, responds to its curve, and
moves to full duty when you test the stop fallback. Do this before leaving the
machine unattended.

## Optional services

### Breathing panel

```bash
sudo bash scripts/install.sh --enable-panel-breathing
```

Breathing replaces the static panel unit and writes the embedded controller more
often. It can also change the apparent brightness of other front LEDs. Use the
static service unless the effect is worth the extra EC traffic.

### Disk LEDs

Map one physical bay at a time in `configs/disk-leds.conf`. Use a whole-device
kernel name and never infer bay order from enumeration. Once the map has been
checked on the chassis:

```bash
sudo bash scripts/install.sh --enable-disk-leds
```

The installer refuses to enable the service when the repository config has no
active slot mapping. Install `smartmontools` before enabling this service: the
installer adds no packages, and when `/usr/sbin/smartctl` is missing the daemon
reports SMART as unavailable. Unknown SMART does not light a healthy green LED.

### USB Copy button

This command enables the listener while leaving every action blocked:

```bash
sudo bash scripts/install.sh --enable-copy-button
```

Grant only the action you intend to use:

```bash
sudo bash scripts/install.sh --enable-copy-button --allow-apt-refresh
sudo bash scripts/install.sh --enable-copy-button --allow-apt-refresh --allow-dist-upgrade
```

### Power button

```bash
sudo bash scripts/install.sh --enable-power-button
```

The installer adds a logind rule so the custom listener receives the key event,
but reboot and poweroff remain blocked. Either action needs another flag:

```bash
sudo bash scripts/install.sh --enable-power-button --allow-reboot
sudo bash scripts/install.sh --enable-power-button --allow-poweroff
```

The Copy listener accepts the same reboot and poweroff flags. Do not grant them
without console access, a tested backup, and a maintenance window.

## Identity check

Every installed entrypoint that writes hardware checks DMI before importing its
implementation. Use those launchers or `python3 -m proxnap.runtime ENTRYPOINT`;
the byte-preserved PWM module is an internal implementation, not an alternate
installation interface. The accepted strings are
`TVS-1288X` and `TVS-H1288X`; the second is an accepted DMI identity from
the same 12-bay chassis family, not evidence that every hardware revision has
been validated. If DMI is blank or generic, the guard accepts only the qnap8528
VPD pair `Q05W0` mainboard plus `Q05K0` backplane. A specific unsupported DMI
value wins over VPD, so a TVS-h1688X is refused even if its VPD data is
inconsistent.

## Remove Proxnap

```bash
sudo bash scripts/install.sh --uninstall
```

Removal stops and disables the units, requests full speed on both fan zones,
removes installed code and logind changes, and keeps `/etc/proxnap`. To remove
the configuration too:

```bash
sudo bash scripts/install.sh --uninstall --purge-config
```

Removal never updates packages, reboots, or powers off the machine.

## Configuration upgrade and compatibility

Code is installed once under `/opt/proxnap/proxnap`; thin commands in
`/usr/local/sbin` preserve `proxnap-fan-control`, `proxnap-lcd`,
`proxnap-copy-update`, `proxnap-power-button` and the public LED commands.
`proxnap-buttons --kind copy|power` dispatches to those same state machines.
`--failsafe` is translated to the fan core's `--safe`. LCD `--off` and a missing
`run` subcommand remain supported through the guarded launcher.

The strict fan parser does not silently accept first-release keys such as
`stall_cycles`. Review retained local configs against `configs/fan-control.conf`
and validate before changing services:

```bash
/usr/local/sbin/proxnap-fan-control --config /etc/proxnap/fan-control.conf --check-config
```

Preserved configuration does not imply unchanged semantics for old click
thresholds. Read [front-panel controls](FRONT_PANEL.md) before enabling actions.
The Copy backend accepts old `apt_refresh_enabled`/`apt_upgrade_enabled` keys
only when their new `short_refresh_enabled`/`upgrade_enabled` equivalents are
absent. Every installer run writes the new opt-ins explicitly from its flags.

## Optional telemetry

`proxnap-metrics.service` is installed but never enabled automatically. Populate
`/etc/proxnap/metrics.env` locally with an InfluxDB 2.x endpoint, organization,
bucket and token before deliberately enabling it. The installer sets mode 0600
on a newly created environment file. No endpoint or token is shipped. Measurement
names start with `proxnap_`; `PROXNAP_METRICS_SLOT_MAP` optionally supplies a local
JSON object mapping device identities to slot labels. Do not commit a real map.

The temperature-aware USB LED command is manual-only: it must not run alongside
status/Copy writers for the same LED. Legacy ACPI helpers have no service/CLI
activation and are not a substitute for PWM thermal control.

## Offline file staging

Set `TMPDIR` to your runtime's dedicated scratch directory first. This path
executes the **same payload writer** as installation, but returns before host
preflight, unit activation, package commands, logind or hardware access:

```bash
PYTHONDONTWRITEBYTECODE=1 bash scripts/install.sh --destdir="$TMPDIR/proxnap-stage"
```

The destination must be a non-symlink child of `TMPDIR`; `/`, symlinked paths and
staged uninstall are refused. The shared writer validates the effective retained
fan config using the exact pure parser definitions before its first payload write;
invalid legacy keys/floors must be reviewed rather than silently migrated. Staging installs no driver source, invokes no
systemctl/modprobe/apt command and enables no services. It is file-layout and
configuration evidence, not permission to run the staged daemons on a host.
