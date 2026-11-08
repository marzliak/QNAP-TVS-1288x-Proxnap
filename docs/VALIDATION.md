# Validation

## Evidence boundaries

Physical observations in [REFERENCE_SYSTEM.md](REFERENCE_SYSTEM.md#operating-record)
belong to the dated reference installation, not an endurance test of this entire
public package. Source consolidation and passing offline tests do not install any
new code, update the driver, prove quieter fans or authorize thermal load tests.

| Area | Public candidate evidence | Still outside offline acceptance |
|---|---|---|
| Fan core | byte-preserved reference implementation; fixture config/curves/floors, missing sensors, inventory/cache, immediate rise, descent, quantized PWM readback and fault/stop paths | physical response, acoustic cause and continuous ownership |
| Optional RPM diagnostics | individual missing/stalled/recovered tachometer regressions | not enabled as continuous PWM-loop polling |
| LCD | bounded fragmented serial parser; real scratch flocks across cooperating writers; startup, disabled, idle, UI/feedback and cleanup ownership; stale/unknown/recovery cache | UART and target UI acceptance |
| Copy | actual AST-selected loop with paired events, capture-time confirmation barriers, flood/replay/error negatives and update cleanup | real package/power work |
| Power | actual AST-selected selector loop with independent sources, aliases, fragment/drop/SYN/reconnect handling | logind and physical shutdown sequencing |
| Status/metrics | malformed/missing/partial/stale/future source contracts; known critical preservation; SMART exit semantics and no unknown-to-healthy conversion | real InfluxDB/server health and device collection |
| Disk/panel/blue | mapped namespace validation, tri-state SMART, activity recovery, timing/brightness and optional pulse policies | physical bay/color/timing acceptance |
| Installation | exact shared file payload staged beneath scratch; default opt-ins, preserving/mutating config, complete module/launcher/unit packaging and symlink/refusal negatives | live installation, service changes or target upgrade |
| Driver experiments | separate explicit-source RAM harnesses in `driver-fixes/` | not applied or executed against hardware by the userspace installer |

## Run offline userspace checks

Use a dedicated scratch path, not a system temporary directory or the published
source tree. Set `TMPDIR` to your runtime-provided scratch directory first.

```bash
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$PWD/src"
python3 -B -m pytest -p no:cacheprovider
ruff check src tests scripts
bash -n scripts/install.sh
python3 -B -c 'import ast,pathlib; [ast.parse(p.read_text()) for p in pathlib.Path("src").rglob("*.py")]'
```

The inherited regression modules are also stdlib `unittest` compatible. Run the
eight named modules separately when pytest is unavailable; the four original
public function-test files still require pytest. No daemon is imported merely to
patch it afterward: production-capable fixture execution uses selected AST nodes,
private module proxies, mandatory inert effects and scratch-only files. Historical
private Git objects are not required to reproduce public regressions.

## Deployment differences

The fan implementation preserves the reference source bytes. Installed wrappers,
packaging, action defaults and chassis guard are public integration layers, not
proof that the reference host consumes them. The public status/LCD/Copy/Power code
retains the recent offline-tested behavior while replacing host-specific defaults
and routing power actions through public opt-ins. Metrics labels use `proxnap_` and
operator-supplied slot mapping, not a private reference inventory.

Legacy first-release fan keys such as `stall_cycles` are intentionally not silently
accepted by the stricter PWM parser. Review existing configuration against the
shipped templates and validate it before any target maintenance. There is no
automatic configuration migration or physical deployment in source consolidation.
