# Hardware support

## Models

| Model | Bays | Status |
|---|---:|---|
| QNAP TVS-1288X | 12 | Tested reference platform |
| QNAP TVS-h1688X | 16 | Not tested; refused by the runtime guard |

The runtime also accepts `TVS-H1288X` as a DMI identity from the same 12-bay
chassis family. That identity is part of the model guard, not a claim that every
hardware revision has been validated.

A similar embedded controller does not make another QNAP model compatible. Fan
zones, temperature inputs, LED numbering, and button events can differ even
between related chassis.

The reference TVS-1288X runs a Xeon W-1290P in place of its original W-1250,
128 GB of DDR4-2667 ECC memory, and Proxmox VE 9.x. The complete build, including
storage and BIOS settings, is in [REFERENCE_SYSTEM.md](REFERENCE_SYSTEM.md) and
[BIOS.md](BIOS.md).

## Standard Linux hardware

Most platform I/O needs no Proxnap daemon. On the reference system, Linux and
Proxmox use the Intel xHCI USB controller, both AHCI storage controllers, the
installed PCIe network and storage devices, and all five NVMe SSDs with their
normal upstream drivers.

All onboard USB ports were exercised during bring-up. The validated paths cover
USB 2.0 and USB 3.x enumeration, USB-C, hubs, keyboards and receivers, and UAS
mass storage. A 14 TB USB disk is in operational use as a separately mounted
backup device.

The installed PCIe configuration exposes four 2.5 GbE interfaces, two 10 GbE
interfaces, an additional AHCI controller, and five NVMe controllers. Those
NVMe devices are in active ZFS roles: mirror members, a mirrored special vdev,
and L2ARC. This demonstrates the installed configuration, not compatibility
with every expansion card.

Proxnap deliberately does not own these standard USB, PCIe, SATA, network, or
NVMe drivers. It adds the QNAP-specific chassis controls that Linux does not
provide by itself.

## Driver version

The reference machine uses the external `qnap8528` driver at commit
`b75d5251b44a8a4b15b4e0846404f255a96fdbfa`. This project does not bundle that
module. A later commit should be treated as a new hardware combination until the
fan, LED, input, and identity interfaces have been checked again.

## Fans and temperatures

`qnap8528` exposes PWM duty as an integer from 0 to 255. Configuration curves use
percentages.

- `pwm1` controls the rear zone. Its tachometers are `fan1_input`, `fan2_input`,
  and `fan3_input`.
- `pwm7` controls the CPU zone. Its tachometers are `fan7_input` and
  `fan8_input`.

The controller reads CPU package temperature from `coretemp` and can fall back
to `qnap8528/temp1_input`. Rear-fan policy can use SATA drive, chipset, DIMM, and
qnap8528 system temperatures (`temp6_input` through `temp8_input`).

The rear fans on the reference chassis were unreliable at very low duty. The
shipped floors, 40% rear and 50% CPU, are cautious starting values. Check every
tachometer through a cold start and a warm load before lowering them.

## LCD

- 16 columns by 2 rows;
- `/dev/ttyS1`, 1200 baud, 8 data bits, no parity;
- command prefix `0x4d`;
- button frame `53 05 00 <mask>`;
- upper/confirm mask `0x01`;
- lower/next mask `0x02`.

## LEDs

The driver exposes these LED class names:

- `qnap8528::status`: `1` is green and `2` is red;
- `qnap8528::usb`: `1` turns on the blue USB Copy LED;
- `qnap8528::panel_brightness`: global brightness from 0 to 100;
- `qnap8528::hdd1` through `qnap8528::hdd8`;
- `qnap8528::ssd1` through `qnap8528::ssd4`;
- `qnap8528::m2ssd1` and `qnap8528::m2ssd2`.

Bay LEDs use `0` for off, `1` for green, and `2` for red. Panel brightness also
changes how bright the other lit LEDs appear. Breathing mode writes the embedded
controller continuously, so the default is a fixed brightness.

## Buttons

The USB Copy button appears as `platform-qnap8528-event` and reports `BTN_2`
(`0x102`) on the tested chassis. The power button reports `KEY_POWER` (`116`).
Press and release arrive immediately, so the daemon uses click sequences rather
than press duration.

Input nodes can move after firmware or kernel changes. Check them with `evtest`
before enabling either listener.

## Outside this project

Proxnap does not manage storage-controller watchdogs, arbitrary embedded
controller registers, rescue workflows, dashboards, or host monitoring. Those
jobs need their own tools and failure handling; they do not belong in a chassis
control daemon.
