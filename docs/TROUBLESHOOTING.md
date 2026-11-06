# Thermal troubleshooting

If an expected fan stops, a temperature source disappears, or PWM readback
fails, use the [recovery procedure](SAFETY.md#recovery). Keep the fan controller
running while investigating ordinary noise or sensor-reporting issues. Do not
lower fan floors or disable firmware protection to silence an alarm.

## Establish what is running

Record the running kernel and the chassis services:

```bash
uname -r
systemctl show proxnap-fan-control proxnap-lcd proxnap-status-led \
  -p Id -p ActiveState -p SubState -p NRestarts
journalctl -u proxnap-fan-control -n 50 --no-pager
```

These are read-only checks. An active process is only one check: confirm live
temperatures and each expected tachometer as well.

## Read PWM and RPM together

Locate qnap8528 by its name, since the `hwmon` number can move:

```bash
for hw in /sys/class/hwmon/hwmon*; do
  test -r "$hw/name" || continue
  test "$(cat "$hw/name")" = qnap8528 || continue
  for name in pwm1 pwm7 fan1_input fan2_input fan3_input fan7_input fan8_input; do
    test -r "$hw/$name" || continue
    printf '%s=' "$name"
    cat "$hw/$name"
  done
done
```

The PWM scale is **0 to 255**. A raw value of 255 requests full duty; it does
not mean 255 percent. Compare raw sysfs values with the documented scale if a
tool displays a surprising percentage. RPM reports what the tachometer sees,
while PWM reports the requested control value.

Keep all five fans in view: `fan1`, `fan2`, and `fan3` are the rear zone;
`fan7` and `fan8` are the CPU zone. A nonzero reading from one fan does not
prove that the others are turning.

## Short fan pulses or repeated speed changes

Brief rear-fan speed increases have been observed on the reference unit while
the reported PWM stayed unchanged. Operational checks also found that the
frequency of EC access could affect the pulses. These are observations from
one chassis, not a manufacturer-confirmed explanation for every speed change.

Check temperature and RPM across several samples, then compare the fan log's
requested duty with the raw readback. Look for another program writing the
same EC interfaces. Proxnap's advisory lock only coordinates writers that use
that lock.

Static panel brightness is the default because breathing mode writes the EC
repeatedly. Extra LED effects can add controller traffic; a quieter-looking
PWM log alone does not establish steadier airflow. Do not test this by
stopping the primary fan controller or bypassing its safeguards.

## Sensor disagreement and alarms

The controller uses `coretemp` for CPU package temperature when available;
qnap8528 provides a fallback. Readings can differ, especially when sampled at
different times. Compare sensor identity and timestamps before deciding that
one value is wrong.

Some reference DIMM sensor reports showed alarm limits set to zero. An alarm
against a zero limit is not, by itself, evidence that the memory crossed a
valid thermal limit. Confirm the part's limits and the sensor configuration;
do not hide the alarm by changing thresholds without that check.

The default rear policy, `storage`, considers more than HDD temperature.
Chipset, DIMM, and system sensors can therefore raise rear-fan duty while the
drives remain cool. Use `hdd` only after confirming thermal coverage for every
populated bay and accepting that the other sensors no longer drive that curve.
Keep the emergency response intact.

## CPU power policy and noise

Read the policy before comparing temperature or acoustic behavior:

```bash
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_driver
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference
cat /sys/devices/system/cpu/intel_pstate/no_turbo
cat /sys/devices/system/cpu/intel_pstate/hwp_dynamic_boost
```

Some files depend on the active driver and may be absent. The reference check
on 6 November used `intel_pstate`, `powersave`, EPP `balance_power`, Turbo on
(`no_turbo=0`), and HWP dynamic boost off. This is the observed configuration,
not a preset applied by the public installer.

Do not infer that Turbo is disabled from the governor name. Workload and power
policy belong in any comparison of fan noise or CPU temperature. No acoustic
measurement, sustained stress test, or power-profile change was performed for
the November snapshot. The [BIOS profile](BIOS.md) describes the reference
firmware settings; [validation](VALIDATION.md) separates those observations
from tests of the public package.
