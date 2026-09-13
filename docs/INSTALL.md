# Installation

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
reads every mapped bay as a SMART failure and blinks it red.

### USB Copy button

This command enables the listener while leaving every action blocked:

```bash
sudo bash scripts/install.sh --enable-copy-button
```

Grant only the action you intend to use:

```bash
sudo bash scripts/install.sh --enable-copy-button --allow-apt-refresh
sudo bash scripts/install.sh --enable-copy-button --allow-dist-upgrade
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

Every command that writes hardware checks DMI first. The accepted strings are
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
