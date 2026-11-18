# Optional qnap8528 driver validation

This directory is separate from the userspace control package. The harnesses and EC-derived code retain their GPL-2.0-or-later SPDX terms; see COPYING. The root MIT license does not relicense these files. Retain original notices when supplying external driver source. No kernel module, vendor firmware or captured external source is distributed here.

## Alternatives, not a patch stack

The pwm-range-validation patch rejects signed negative PWM before8-bit conversion; only the hwmon callback changes. The fan-host-serialization patch additionally extends the mutex across command pairs. **The compound patch is experimental, not recommended by default.** Exercised RAM models expose continuous logical holds near40seconds for PWM pairs and50seconds for RPM pairs under delayed-success/timeout conditions. Those counters are neither physical latency nor scheduler/queue limits. Original separate acquisitions are shorter.

Apply either patch only to the exact pinned1.22source, never one over the other. A host mutex does not freeze autonomous firmware, latch RPM bytes, guarantee ACK or roll back partial commands. Do not shorten retries to hide the modeled delay. Neither patch is a demonstrated fix for noise/oscillations.

## Hardware-free reproduction

Supply lawfully obtained sourceSHA256`5a822199043a65298d486c26e1e3b5ab6b8e816d98013113bebd8f2c83f137d3` and headerSHA256`1dac171f80378a72b3458fd5ce31b24a0bda2b30989175a8af68b31c0da7ed38`. Inputs are mandatory; no download/hardware fallback. Set TMPDIR to an existing private runtime scratch directory; strict runners reject a shared/tmp root.

```sh
python3 -B driver-fixes/test_driver_fans.py --source "$SOURCE"
python3 -B driver-fixes/test_driver_latency.py \
  --source "$SOURCE" --source-sha256 5a822199043a65298d486c26e1e3b5ab6b8e816d98013113bebd8f2c83f137d3 \
  --header "$HEADER" --scratch-root "$TMPDIR"
python3 -B driver-fixes/test_driver_pwm_validation.py \
  --source "$SOURCE" --source-sha256 5a822199043a65298d486c26e1e3b5ab6b8e816d98013113bebd8f2c83f137d3 \
  --scratch-root "$TMPDIR"
```

The selected literal C definitions compile with mandatory port/delay/mutex mocks and UBSan. No realEC IO or module load occurs. The range harness proves the rest of the source unchanged and requires the unfixed negative control to fail. The transport harness exercises failure phases, first errors,50microsecond epilogues, release and partial effects. Internal CHECK totals are not counts of distinct scenarios. Captured unsigned type-limit warnings remain visible where specifically exempted from-Werror; other warnings remain fatal.

## Panel brightness mask — analysis-only candidate

`qnap8528-1.22-panel-mask.patch` changes only the final `0x245` mask from
`OR 0xef` to `AND 0xef`: clear bit 4 while preserving the other bits from that
read. The original successful path yields `0xff` for every initial byte. The
candidate is a semantic mask correction, **not a fan-pulse fix or a deployable
brightness-animation driver**. No installer applies it.

Use the same pinned original source above; do not stack patches without separate
validation. Run the RAM-only callback harness explicitly:

```sh
python3 -B driver-fixes/test_driver_panel_mask.py \
  --source "$SOURCE" \
  --source-sha256 5a822199043a65298d486c26e1e3b5ab6b8e816d98013113bebd8f2c83f137d3 \
  --scratch-root "$TMPDIR"
```

The harness compiles the literal baseline/candidate callback with UBSan and
mandatory inert register mocks. It exercises brightness 0..100 against all 256
initial control-register bytes, unregister preservation, every callback IO error
phase, and a baseline negative control. It also deliberately reproduces unresolved
partial-effect and stale-read problems: failure after setting bit 4 has no cleanup,
and an injected register change between read and write is overwritten. That
injection is an adversarial model, not evidence of a second live writer to this
register. Existing per-command transport locking is unchanged; full transaction
arbitration and failure recovery are not established by this patch.

**Do not install/load or animate real hardware based on these results.** A full
module build, controller semantics, bounded recovery and concurrency/latency
validation remain separate gates. No continuous brightness writer, fan change,
service restart or physical acceptance is included.

A full module build also needs exact target headers and separate vermagic, symbolCRC, compiler and signing checks. Static success is not runtime lockdep, physical acceptance or permission to load. Do not execute modules_install, depmod, reload or reboot here. The userspace installer must never apply these optional patches automatically.
