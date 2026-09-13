# Reference system

This is the machine used to develop and run Proxnap. Capacities below use the
values reported by Linux and Proxmox. The inventory was refreshed on 12
September 2026.

## Chassis, CPU, and platform

| Item | Installed configuration |
|---|---|
| Chassis | QNAP TVS-1288X, 8 x 3.5-inch bays and 4 x 2.5-inch bays |
| CPU | Intel Xeon W-1290P, 10 cores / 20 threads, 3.70 GHz base |
| Original CPU | Intel Xeon W-1250, replaced by the W-1290P |
| Firmware | American Megatrends 5.13, build date 2 April 2021 |
| BIOS interface | AMI Aptio Setup 2.21.1278 |
| OS | Proxmox VE 9.x on Debian 13; 9.2.18 in the latest snapshot |
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

The capacity column combines the top-level dataset's used and available values.
It is the usable dataset space seen by ZFS, not raw disk capacity.

| Pool | Topology | Used | Available | Use | Main properties |
|---|---|---:|---:|---:|---|
| `rpool` | mirror, 2 x 512 GB SATA SSD | 31.69 GiB | 414.56 GiB | 7.10% | boot/root; compression on; 128K records |
| `vmpool` | mirror, 2 x 1 TB NVMe | 104.08 GiB | 795.20 GiB | 11.57% | VM data; `zstd-3`; 128K records |
| `qlc-cold` | mirror, 2 x 1 TB SATA SSD | 2.39 MiB | 899.25 GiB | <0.01% | replica data; `zstd-9`; 1M records |
| `fast4` | stripe, 2 x 4 TB HDD | 10.55 MiB | 7.12 TiB | <0.01% | bulk tier; `lz4`; 1M records |
| `tank` | RAIDZ1, 6 x 16 TB HDD | 10.90 TiB | 58.73 TiB | 15.66% | main data; properties vary by dataset |

`tank` also has a mirrored special vdev made from the two Samsung 970 EVO Plus
SSDs. Metadata and selected small blocks can land there. The ADATA 2 TB NVMe is
used as L2ARC. Loss of both special-vdev members would lose the pool, so the
special vdev must be backed up and monitored like the data vdev.

`fast4` is a stripe. Either disk failing loses the pool. It is suitable only for
data that exists elsewhere or can be recreated.

## Proxmox storage view

The current Proxmox definitions are listed below. `tank` is an online ZFS pool
but is not registered as Proxmox storage. External backup targets are listed by
role.

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

The longest completed boot interval in the retained system login history ran
from 20 July 2026 at 02:46 BRT to 1 September 2026 at 02:21 BRT: **42 days,
23 hours, and 35 minutes**. This is a verifiable record from the history still
present on the machine, not a lifetime maximum.

Latest live check: 12 September 2026 at 20:25 BRT:

| Check | Result |
|---|---|
| Uptime | 7 days, 1 hour, 48 minutes; boot time 5 September 2026, 18:37 BRT |
| ZFS | all five pools healthy |
| systemd | zero failed units |
| Guests | one running, two stopped |
| Load average | 3.17 / 3.41 / 3.74 on 20 logical CPUs |
| Host memory | 84.98 GB used of 134.90 GB reported by Proxmox, including host cache |
| Host swap | disabled, 0 bytes configured |
| Hottest CPU package reading | 70 C from `coretemp` |
| QNAP system temperature inputs | 31 C, 29 C, and 25 C |
| Rear fans | 1755, 1873, and 1904 RPM; `pwm1=102` (40%) |
| CPU fans | 1376 and 1392 RPM; `pwm7=137` (54%) |

The qnap8528 module loader, fan controller, LCD, status LED, static panel LED, and
USB Copy service were active. Breathing mode, disk-LED monitoring, and the custom
power-button service were disabled. That service mix reflects the reference
machine, while a fresh install keeps both button listeners disabled.
