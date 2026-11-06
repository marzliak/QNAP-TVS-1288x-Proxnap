# Reference system

This is the reference machine for the TVS-1288X conversion. The physical
inventory was recorded on 12 September 2026; the software, ZFS capacity, and
operating snapshot were refreshed on 6 November 2026. Capacities use the values
reported by Linux, ZFS, and Proxmox.

## Chassis, CPU, and platform

| Item | Installed configuration |
|---|---|
| Chassis | QNAP TVS-1288X, 8 x 3.5-inch bays and 4 x 2.5-inch bays |
| CPU | Intel Xeon W-1290P, 10 cores / 20 threads, 3.70 GHz base |
| Original CPU | Intel Xeon W-1250, replaced by the W-1290P |
| Firmware | American Megatrends 5.13, build date 2 April 2021 |
| BIOS interface | AMI Aptio Setup 2.21.1278 |
| OS | Proxmox VE 9.x on Debian 13; 9.2.26 in the November snapshot |
| Kernel | 7.0.14-15-pve |
| Network controllers | 4 x Intel I225-V and 2 x Intel X550 |
| SATA controllers | Intel Comet Lake AHCI and ASMedia ASM1164 AHCI |

The running kernel sees the Intel UHD Graphics P630, but the stable boot profile
does not load i915. After the W-1290P swap, the reference machine also stopped
producing usable video during firmware setup, so BIOS work is done through the
rear 3.5 mm serial console. [BIOS.md](BIOS.md) documents the cable and both setup
modes.

## Memory

All four slots are populated. The platform reports single-bit ECC, a 72-bit
total width, 64-bit data width, two ranks per DIMM, and 1.2 V configured voltage.

| Slot | Capacity | Manufacturer | Part number | Type | Configured speed |
|---|---:|---|---|---|---:|
| ChannelA-DIMM0 | 32 GB | Kingston | `9965745-035.A00G` | DDR4 ECC, 2Rx8 | 2667 MT/s |
| ChannelA-DIMM1 | 32 GB | Kingston | `9965745-035.A00G` | DDR4 ECC, 2Rx8 | 2667 MT/s |
| ChannelB-DIMM0 | 32 GB | Kingston | `9965745-041.A00G` | DDR4 ECC, 2Rx8 | 2667 MT/s |
| ChannelB-DIMM1 | 32 GB | Kingston | `9965745-041.A00G` | DDR4 ECC, 2Rx8 | 2667 MT/s |

Total installed memory is 128 GB. The firmware's SMBIOS table still advertises a
64 GB maximum, which is inconsistent with the working 128 GB configuration. The
four DIMMs are detected and in service despite that stale limit.

## Physical storage

| Qty. | Device | Nominal capacity | Current role |
|---:|---|---:|---|
| 6 | WD Red Pro `WD161KFGX` HDD | 16 TB each | `tank` RAIDZ1 data vdev |
| 2 | WD Red `WD40EFRX` HDD | 4 TB each | `fast4` stripe |
| 2 | Kingston KC600 `SKC600512G` SATA SSD | 512 GB each | mirrored Proxmox boot pool |
| 2 | Samsung 860 QVO SATA SSD | 1 TB each | `qlc-cold` mirror |
| 2 | Samsung 970 EVO Plus NVMe SSD | 1 TB each | mirrored `tank` special vdev |
| 1 | Samsung 970 EVO NVMe SSD | 1 TB | one side of the `vmpool` mirror |
| 1 | SanDisk Extreme NVMe SSD | 1 TB | other side of the `vmpool` mirror |
| 1 | ADATA SX8100 NVMe SSD | 2 TB | `tank` L2ARC cache |
| 1 | Seagate Backup+ Hub USB HDD | 14 TB | ext4 backup disk |

The USB disk is mounted separately and is not a member of a ZFS pool.

## Validated I/O

The reference build retains the platform interfaces needed for normal use:

- every onboard USB port was exercised during bring-up; USB 2.0, USB 3.x,
  USB-C, HID, hub, and UAS mass-storage paths were observed;
- the 14 TB USB backup disk is mounted and in operational use;
- the installed PCIe storage and network devices enumerate with Linux drivers;
- all five NVMe SSDs are visible and hold active ZFS data, special-vdev, or cache
  roles;
- both SATA/AHCI controllers and the installed disks are visible, and bay
  hot-plug was tested during conversion;
- both QNAP fan zones and all five expected tachometers have live control and
  readback;
- the chassis LCD, its two buttons, status/Copy/panel LEDs, selected drive and
  M.2 activity paths, USB Copy input, and power input were physically mapped;
- the simple buzzer produced short, long, and repeated patterns when tested with
  the original QNAP HAL.

The buzzer result proves the hardware path. Proxnap does not drive the buzzer.
Standard USB, PCIe, SATA, Ethernet, and NVMe operation likewise comes from
Linux rather than from Proxnap.

## ZFS layout

Used and available space come from each pool's top-level dataset. The use
percentage is `used / (used + available)`, not the raw pool allocation ratio.
The figures below were read on 6 November 2026 at 19:56 BRT.

| Pool | Topology | Used | Available | Use | Main properties |
|---|---|---:|---:|---:|---|
| `rpool` | mirror, 2 x 512 GB SATA SSD | 44.29 GiB | 401.80 GiB | 9.93% | boot/root; compression on; 128K records |
| `vmpool` | mirror, 2 x 1 TB NVMe | 138.94 GiB | 760.48 GiB | 15.45% | VM data; `zstd-3`; 128K records |
| `qlc-cold` | mirror, 2 x 1 TB SATA SSD | 2.56 MiB | 899.25 GiB | <0.01% | replica data; `zstd-9`; 1M records |
| `fast4` | stripe, 2 x 4 TB HDD | 11.43 MiB | 7.12 TiB | <0.01% | bulk tier; `lz4`; 1M records |
| `tank` | RAIDZ1, 6 x 16 TB HDD | 11.41 TiB | 58.23 TiB | 16.38% | main data; properties vary by dataset |

`tank` also has a mirrored special vdev made from the two Samsung 970 EVO Plus
SSDs. Metadata and selected small blocks can land there. The ADATA 2 TB NVMe is
used as L2ARC. Loss of both special-vdev members would lose the pool, so the
special vdev must be backed up and monitored like the data vdev.

`fast4` is a stripe. Either disk failing loses the pool. It is suitable only for
data that exists elsewhere or can be recreated.

## Proxmox storage view

The Proxmox definitions and figures below are the 12 September 2026 inventory,
not the November capacity snapshot. At that inventory, `tank` was an online
ZFS pool but was not registered as Proxmox storage. External backup targets are
listed by role.

| Storage role | Backend | Total | Used | Used % |
|---|---|---:|---:|---:|
| `local` | directory on `rpool` | 421.33 GiB | 6.77 GiB | 1.61% |
| `local-zfs` | ZFS | 418.77 GiB | 4.21 GiB | 1.01% |
| `vmpool` | ZFS | 899.11 GiB | 103.92 GiB | 11.56% |
| `qlc-cold-backup` | directory on ZFS | 899.25 GiB | 1.00 MiB | <0.01% |
| `fast4` | ZFS | 7.12 TiB | 10.55 MiB | <0.01% |
| external backup | NFS | 3.63 TiB | 732.78 GiB | 19.71% |
| external backup | Proxmox Backup Server | 21.54 TiB | 2.20 TiB | 10.19% |
| external DR backup | Proxmox Backup Server | 21.54 TiB | 2.20 TiB | 10.19% |

The two Proxmox Backup Server rows are separate storage definitions. Matching
reported capacity does not prove that they are independent failure domains.

## Operating record

The current boot began on 5 September 2026 at 18:37 BRT and was still running
at the November check. It has exceeded the longest completed interval in the
retained login history: **42 days, 23 hours, and 35 minutes**, from
20 July 2026 at 02:46 BRT to 1 September 2026 at 02:21 BRT.

Latest live check: **6 November 2026 at 19:56 BRT**:

| Check | Result |
|---|---|
| Uptime | 62 days, 1 hour, 19 minutes; boot time 5 September 2026, 18:37 BRT |
| Proxmox manager / running kernel | 9.2.26 / 7.0.14-15-pve |
| ZFS | all five pools healthy |
| Chassis services | fan control, LCD, and status LED active; zero automatic restarts in their current systemd counters |
| Load average | 2.50 / 2.86 / 2.80 on 20 logical CPUs |
| Host memory | 125.64 GiB total, 65.97 GiB available from Linux meminfo |
| Host swap | disabled, 0 bytes configured |
| CPU package reading | 69 C from `coretemp` |
| QNAP system temperature inputs | 32 C, 30 C, and 27 C |
| Rear fans | 1760, 1868, and 1855 RPM; `pwm1=102` (40%) |
| CPU fans | 1392 and 1376 RPM; `pwm7=137` (54%) |
| CPU power policy | `intel_pstate`, `powersave`, EPP `balance_power`; Turbo enabled, HWP dynamic boost disabled |

The qnap8528 module loader, fan controller, LCD, status LED, static panel LED, and
USB Copy service were active. Disk-LED monitoring and the custom power-button
service were inactive. A fresh install of this package keeps both button
listeners disabled.

The installed chassis controller predates this repository's Python package.
These observations establish the reference machine's operating record, not
long-duration hardware validation of every public daemon or fail-safe path.
The [validation matrix](VALIDATION.md) identifies the checks made on the
hardware and in automated tests. Temperature, load, and capacity are dated
samples; service restart counters do not establish an incident-free history.
