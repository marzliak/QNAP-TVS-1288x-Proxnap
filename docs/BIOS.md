# BIOS configuration

The W-1290P upgrade exposed two problems that were easy to confuse: limited
all-core turbo and platform instability around deep idle, iGPU, and PCIe power
management. The settings below are the profile that has worked on the reference
TVS-1288X. Treat them as one machine's working record. Copy a setting only when
you understand why it is there and can test the result on your own hardware.

The BIOS pages were recorded during a console review on 13 June 2026. The running
system still reported American Megatrends firmware 5.13, dated 2 April 2021, when
checked on 12 September 2026. Linux cannot confirm every setup-menu value, so
check the firmware screens before copying this profile.

## Getting into the BIOS after the CPU swap

On the reference machine, replacing the W-1250 with a W-1290P left Linux working
but removed usable video during firmware setup. The built-in display output
therefore cannot be relied on to see POST or the BIOS. This is an observation
from the tested machine, not a claim that every W-1290P swap behaves the same
way.

The practical access path is the rear **3.5 mm (P2) console port**. Despite the
connector shape, this is an RS-232 console, not an audio socket or a generic
TTL-UART lead.

Required hardware:

- a D-SUB 9-pin female to 3.5 mm stereo-plug console cable;
- a USB-to-RS-232 adapter if the computer has no native serial port;
- a USB keyboard connected directly to the NAS.

Set the computer's serial terminal to:

```text
Speed:       115200 baud
Data bits:   8
Parity:      none
Stop bits:   1
Flow control: none
```

Then:

1. Shut the NAS down cleanly.
2. Connect the P2/DB9 cable to the rear console port and the computer. Add the
   USB-to-RS-232 adapter when needed.
3. Open the correct serial port in PuTTY, `screen`, `minicom`, or another
   terminal before powering the NAS on.
4. Connect the USB keyboard to the NAS itself. The serial terminal provides the
   picture; the directly attached keyboard is the reliable pre-boot input path.
5. Power on the NAS and start pressing the key combination for the setup mode
   you need while POST is running.

Do not use `/dev/ttyS1` for this purpose. In Linux that UART belongs to the front
LCD. BIOS redirection and the persistent Linux maintenance console use the rear
port, which appears to Linux as `/dev/ttyS0` on the tested machine.

### The two BIOS views

QNAP's wording is confusing because both views contain an **Advanced** tab:

1. **Regular setup** — repeatedly press `Del` or `F2` during POST. This opens the
   normal firmware interface and its reduced Advanced page.
2. **Expanded Advanced setup** — press `Ctrl+F1` during startup, while POST is
   still running. This enters the unlocked view with the additional CPU,
   chipset, power-management, VR, and overclocking menus used for this build.
   Pressing `Ctrl+F1` only after entering regular setup may not unlock it.

There is one more nested gate inside the expanded view. Under
**Advanced → OverClocking Performance Menu**, `OverClocking Feature` is a master
switch that reveals processor, memory, GT, ring, uncore, and CPU VR controls.
Enabling that switch to inspect the submenus is not a recommendation to apply an
overclock. On the reference machine it was returned to `Disabled` after review;
the important persistent change was Core/IA VR `TDC Enable = Disabled`.

The access sequence and cable specification follow QNAP's
[3.5 mm console and BIOS procedure](https://www.qnap.com/en-us/how-to/faq/article/how-do-i-enter-the-console-and-bios-on-the-qnap-nas-only-with-3-5mm-console-port).
QNAP's separate
[console-port BIOS update procedure](https://www.qnap.com/en/how-to/faq/article/how-to-update-nas-bios-through-the-console-port)
uses the same cable and serial settings. Updating firmware is outside this
project; do not treat the access instructions above as a flashing guide.

## CPU and virtualization

| Setting | Value |
|---|---|
| Active Processor Cores | All |
| Hyper-Threading | Enabled |
| Intel VMX virtualization | Enabled |
| VT-d | Enabled |
| IOMMU during pre-boot | Enabled without exception list |
| Intel Speed Shift | Enabled |
| Turbo Mode | Enabled |
| Turbo Boost Max 3.0 | Disabled |
| Turbo ratio table | Firmware-detected values; highest ratio 53 |
| CPU C-states | Disabled |
| Timed MWAIT | Disabled |
| Thermal Monitor | Enabled |
| Machine Check | Enabled |
| AES | Enabled |
| SGX | Disabled |
| Intel TXT | Disabled |
| CPU Flex Ratio Override | Disabled |
| Platform PL1 / PL2 override | Disabled / Disabled |
| Power Limit 4 override | Disabled |
| Package Power Limit MSR Lock | Disabled |
| Energy Efficient Turbo | Auto |
| Energy Efficient P-state | Enabled |
| Core/IA VR `TDC Enable` | **Disabled** |

`TDC Enable` was the important performance setting after the CPU swap. Disabling
it raised the measured all-core frequency from about 3.7 GHz to 4.34 GHz under
the same benchmark load. Energy Efficient Turbo and Energy Efficient P-state did
not explain the limit. Keep the remaining Core/IA VR values at their firmware
settings unless a measured test gives a reason to change them.

CPU C-states remain disabled because the machine had fatal machine-check events
while deeper idle and graphics paths were being tested. Thermal Monitor and
Machine Check stay enabled; hiding the errors would not fix the cause.

## PCIe, graphics, and power management

| Setting | Value |
|---|---|
| Native PCIe | Enabled |
| Native ASPM | Disabled when the menu permits it |
| DMI Link ASPM Control | Disabled |
| PCIe Clock Gating | Enabled |
| PCIe function swap | Enabled |
| Above 4 GB MMIO assignment | Disabled |
| Peer Memory Write | Disabled |
| Compliance Test Mode | Disabled |
| Low Power S0 Idle | Disabled |
| DeepSx power policies | Disabled |
| EuP function | Disabled |
| C6DRAM | Enabled |
| Restore AC Power Loss | Last State |
| iGPU / i915 use | Disabled in Linux boot profile |

The machine has previously logged PCIe AER errors on NVMe paths. Disabling ASPM
in firmware and Linux trades a small amount of idle power for a less complicated
link state. Keep the iGPU out of the Linux boot path unless you intend to repeat
the machine-check and PCIe validation from scratch.

## SATA and storage

| Setting | Value |
|---|---|
| SATA controllers | Enabled |
| SATA mode | AHCI |
| SATA test mode | Disabled |
| Hot Plug | Enabled on populated bays |
| Aggressive Link Power Management | Disabled |
| SATA DevSlp | Disabled |
| PUIS | Disabled |
| Spin Up Device | Disabled unless startup current becomes a problem |
| eSATA mode | Disabled for internal bays |
| PCH/SATA/DMI thermal thresholds | Intel/QNAP suggested settings |
| PCH Cross Throttling | Enabled |
| PCH Energy Reporting | Enabled |

Do not switch the controller to Intel RST. ZFS needs direct access to the disks,
and the QNAP bays depend on ordinary AHCI hot-plug behavior.

## Board and firmware safeguards

| Setting | Value |
|---|---|
| BIOS Beep | Enabled |
| RTC Memory Lock | Enabled |
| SPD Write Disable | True |
| BIOS Guard | Disabled |
| BIOS Lock | Disabled |
| Force unlock on GPIO pads | Disabled |
| TCO Timer | Enabled |
| ACPI Debug | Disabled |
| PECI | Enabled; Direct I/O access method |
| PTID support | Enabled |
| ISH controller | Enabled |
| USB overcurrent / lock | Enabled / Enabled |
| XHCI compliance mode | Disabled |

BIOS Guard and BIOS Lock were left off while the profile was still being tuned.
They are not needed for runtime performance. Revisit firmware write protection
only after confirming that future maintenance and recovery procedures still
work.

## Known-good Linux boot profile

The current kernel command line, minus the machine-specific ZFS root selector,
contains:

```text
boot=zfs nvme_core.default_ps_max_latency_us=0 pcie_port_pm=off nvme_core.io_timeout=120 nvme_core.admin_timeout=120 nvme_core.multipath=N console=tty0 console=ttyS0,115200n8 intel_idle.max_cstate=1 pcie_aspm=off nomodeset module_blacklist=i915 modprobe.blacklist=i915
```

The important constraints are:

- `intel_idle.max_cstate=1` limits Linux to shallow CPU idle states;
- `pcie_aspm=off` and `pcie_port_pm=off` keep PCIe links out of low-power states;
- `nvme_core.default_ps_max_latency_us=0` disables NVMe autonomous power saving;
- `nomodeset` and the i915 blacklist prevent integrated-graphics initialization;
- the serial console stays available on `ttyS0` at 115200 baud.

Change one firmware or kernel setting at a time. Reboot, check the kernel log for
machine checks and PCIe AER entries, then repeat the same CPU and storage tests.
Changing several power controls together makes the result impossible to
attribute.
