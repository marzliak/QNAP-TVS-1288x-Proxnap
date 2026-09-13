# QNAP TVS-1288X on Proxmox

Installing Proxmox VE does not have to turn the TVS-1288X front panel into
decoration. Proxnap restores the QNAP-specific controls that disappear with
QTS: both fan zones, the 16x2 LCD and its buttons, chassis and drive LEDs, the
USB Copy button, and the power button.

> [!WARNING]
> This code has been tested on one **QNAP TVS-1288X (12-bay)**. It has not been
> tested on the **TVS-h1688X (16-bay)**. Every hardware-facing command checks the
> chassis identity and refuses the 16-bay model.

## What Proxnap brings back

| Chassis feature | Proxnap behavior |
|---|---|
| CPU and rear fans | independent temperature curves with per-fan stall detection and a two-zone 100% fail-safe |
| 16x2 LCD | rotating date/time, CPU and system temperature, memory, and fan-RPM pages |
| LCD buttons | wake the backlight and advance the page rotation immediately |
| Status LED | solid green when healthy; red for a critical local fault |
| Front-panel lighting | fixed brightness by default, with an optional breathing effect |
| HDD, SSD, and M.2 LEDs | green on activity and flashing red on critical SMART health, after explicit bay mapping |
| USB Copy and power buttons | guarded multi-click actions with a second physical confirmation sequence |

The controls run locally as systemd services and do not depend on a dashboard or
cloud connection. See [front-panel controls](docs/FRONT_PANEL.md) for the exact
LCD pages, light patterns, button behavior, and safety defaults.

## Hardware retained under Proxmox

The conversion did not cost the reference machine its normal chassis I/O.
Standard USB, PCIe, SATA, NVMe, and network operation comes from stock Linux
drivers rather than from Proxnap. The following were validated on the tested
TVS-1288X:

| Area | Validated result |
|---|---|
| USB | all onboard ports tested during bring-up; USB 2.0 and USB 3.x devices enumerate normally, including HID devices, hubs, USB-C operation, and UAS mass storage |
| PCIe | the devices installed in the reference configuration enumerate and bind to Linux drivers, including the dual-port 10 GbE adapter, AHCI storage controller, and NVMe controllers |
| NVMe | all five installed NVMe SSDs are visible and active as ZFS data, mirrored special-vdev, and L2ARC devices |
| SATA and bays | both AHCI controllers, all installed SATA disks, and tested bay hot-plug operate under Linux |
| Network | four Intel 2.5 GbE and two Intel 10 GbE interfaces are detected |
| Fans and sensors | both PWM zones, all three rear-fan tachometers, both CPU-fan tachometers, and the required thermal inputs are working |
| LCD, LEDs, and buttons | LCD text/backlight/buttons, status and USB Copy LEDs, panel brightness, drive/M.2 activity LEDs, USB Copy input, and power input have working Linux paths |
| Buzzer | short, long, and repeated beep patterns were audibly verified with the original QNAP HAL; Proxnap does not drive the buzzer under Proxmox |

"Validated" here refers to this machine and the installed devices, not every
possible peripheral or expansion card. The buzzer is the clearest case: the
hardware works, but Proxnap has no native Linux path to it, so beep control is
not one of its features.

## Reference machine

The working system is not stock:

- Intel Xeon W-1290P, 10 cores and 20 threads, fitted in place of the original
  Xeon W-1250;
- 128 GB of Kingston DDR4 ECC memory, installed as four 32 GB dual-rank DIMMs
  running at 2667 MT/s;
- Proxmox VE 9.x on Debian 13 (9.2.18 in the latest snapshot), with kernel
  7.0.14-15-pve;
- five ZFS pools spanning SATA SSDs, NVMe SSDs, and eight hard disks;
- a separate 14 TB USB backup disk;
- six Intel Ethernet interfaces: four I225-V and two X550.

The main storage pool uses six 16 TB WD Red Pro drives in RAIDZ1, a mirrored
special vdev on two 1 TB Samsung 970 EVO Plus SSDs, and a 2 TB ADATA L2ARC. Boot,
VM, cold-flash, and bulk-fast storage live on separate mirrored or striped pools.
See [reference system](docs/REFERENCE_SYSTEM.md) for the full physical and logical
layout, memory part numbers, pool properties, capacity, and the latest health
snapshot.

### BIOS profile

The stable profile keeps virtualization and turbo enabled but favors PCIe and
thermal reliability over idle power:

- VMX, VT-d, Hyper-Threading, all cores, Speed Shift, Turbo Mode, Machine Check,
  and Thermal Monitor enabled;
- CPU C-states disabled, with `intel_idle.max_cstate=1` in Linux;
- CPU Core/IA `TDC Enable` disabled, which removed the all-core turbo limit seen
  after the W-1290P upgrade;
- SATA in AHCI mode with hot-plug enabled and DevSlp disabled;
- DMI/PCIe ASPM disabled, backed by `pcie_aspm=off` and `pcie_port_pm=off`;
- the integrated GPU left unused, with i915 blacklisted in Linux;
- power-loss recovery set to `Last State`.

On the reference machine, the W-1290P swap removed usable BIOS video output.
Setup is viewed through the rear 3.5 mm (P2) RS-232 console at 115200 8N1, with a
USB keyboard attached directly to the NAS. `Del` or `F2` opens regular setup;
`Ctrl+F1` during POST opens the expanded Advanced view. The cable, timing, two
BIOS views, and exact settings are documented in
[BIOS configuration](docs/BIOS.md). This is a record of one working machine,
not a universal profile for every TVS-1288X.

## Operating record

The longest completed boot interval in the retained system login history is
**42 days, 23 hours, and 35 minutes**: 20 July 2026 at 02:46 BRT through
1 September 2026 at 02:21 BRT. This is the longest verifiable interval in the
history still present on the reference machine, not a claim about its entire
lifetime.

At the latest check, on 12 September 2026 at 20:25 BRT, the machine had been up
**7 days, 1 hour, and 48 minutes** (booted 5 September 2026 at 18:37 BRT), with
all five ZFS pools healthy and no failed systemd units. The hottest CPU package
reading was 70 C, rear fans were turning at 1755-1904 RPM, and all local pools
were below 16% dataset-space use. Load, memory, per-fan RPM, temperatures, and
pool capacity are tabulated in
[reference system](docs/REFERENCE_SYSTEM.md#operating-record). These are
point-in-time figures, not sizing recommendations.

## Hardware controls

- `pwm7` drives the CPU fan zone (`fan7` and `fan8`).
- `pwm1` drives the three rear fans (`fan1`, `fan2`, and `fan3`).
- `/dev/ttyS1` carries the 1200-baud QNAP/ICP protocol for the 16x2 LCD.
- `qnap8528` exposes temperatures, PWM, LEDs, and front-panel input.
- Static panel brightness is the default. Breathing mode is optional.
- Disk LEDs require a manual bay map before they can be enabled.
- USB Copy and power-button listeners are disabled by default.

## Driver

The hardware interface comes from the external
[`0xGiddi/qnap8528`](https://github.com/0xGiddi/qnap8528) kernel module. The module
is not bundled here. The reference machine uses commit
`b75d5251b44a8a4b15b4e0846404f255a96fdbfa`; later revisions may expose different
interfaces and need a fresh hardware test. The pinned DKMS procedure is in
[installation](docs/INSTALL.md#install-the-qnap8528-driver).

## Safety defaults

A missing sensor, failed PWM write, readback mismatch, stalled fan, daemon error,
or orderly daemon stop requests full speed on both fan zones. Fan faults also
create `/run/proxnap-fan-fault` and request a red status LED. All of that
depends on the `qnap8528` hwmon interface being present. If it disappears,
software cannot command the fans at all, and firmware thermal protection is the
only backstop left.

Package refresh, distribution upgrade, reboot, and poweroff are disabled in the
shipped configuration. Enabling a button listener does not enable those actions.
Each action requires a separate opt-in and a physical confirmation sequence.
The installer never performs any of them.

Read [hardware support](docs/HARDWARE_SUPPORT.md) and the
[safety model](docs/SAFETY.md) before installing. If the fans are already
misbehaving, go straight to the [recovery procedure](docs/SAFETY.md#recovery).
The work Proxnap deliberately leaves alone is listed under
[outside this project](docs/HARDWARE_SUPPORT.md#outside-this-project).

## Install

```bash
bash scripts/install.sh --preflight
bash scripts/install.sh --dry-run
sudo bash scripts/install.sh
```

The default install enables driver loading, fan control, LCD output, the status
LED, and static panel brightness. [INSTALL.md](docs/INSTALL.md) covers optional
services, action opt-ins, verification, and removal.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e '.[test]'
ruff check .
python3 -m pytest
bash -n scripts/install.sh
python3 -m compileall -q src tests
```

The test suite uses mocks and temporary files, so it does not touch QNAP
hardware. A clean test run cannot replace validation on the physical chassis.
[Architecture](docs/ARCHITECTURE.md) describes the service layout, the lock
model, and the trust boundary.

## License

MIT. See [LICENSE](LICENSE).
