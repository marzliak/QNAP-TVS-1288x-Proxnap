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

A full module build also needs exact target headers and separate vermagic, symbolCRC, compiler and signing checks. Static success is not runtime lockdep, physical acceptance or permission to load. Do not execute modules_install, depmod, reload or reboot here. The userspace installer must never apply these optional patches automatically.
