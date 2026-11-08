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
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
python3 -B -m pytest -p no:cacheprovider
ruff check --no-cache src tests scripts
bash -n scripts/install.sh
python3 -B -c 'import ast,pathlib; [ast.parse(p.read_text()) for p in pathlib.Path("src").rglob("*.py")]'
```

The inherited regression modules are also stdlib `unittest` compatible. Run the
eight named modules separately when pytest is unavailable; the four original
public function-test files still require pytest. No daemon is imported merely to
patch it afterward: production-capable fixture execution uses selected AST nodes,
private module proxies, mandatory inert effects and scratch-only files. Historical
private Git objects are not required to reproduce public regressions.

## LCD scrolling compatibility checks

`tests/test_lcd_status_scroll.py` exercises the actual AST-selected snapshot
parser, presentation/timeline, settings, UI and daemon loop. It reuses mandatory
inert input/UART/worker/effect providers, fake monotonic clocks, scratch flocks and
unique RAM-only abstract sockets. There is no production hardware/control call.

The suite checks default three-argument legacy compatibility; exact initial,
step, final-window and end-pause boundaries; automatic cause progression; Unicode
ASCII normalization; warning/critical/unknown priority and stale/invalid negatives;
exact per-unit aliases and faithful fallbacks; finite/clamped settings; text/count/
duration caps with explicit omission notices; changed-frame-only writes; fixed
HOME-frame return; cancellation, disabled/rebound inputs/endpoints, external
ownership and fresh navigation boundaries. The existing Copy-to-IPC-to-LCD and
maintenance dispatch regressions also run unchanged with the default pager.
Physical readability, cadence and return still require the operator's panel test.

## Deployment differences

The fan implementation preserves the reference source bytes. Installed wrappers,
packaging, action defaults and chassis guard are public integration layers, not
proof that the reference host consumes them. The public status/LCD/Copy/Power code
retains the recent offline-tested behavior while replacing host-specific defaults
and routing power actions through public opt-ins. Metrics labels default to `proxnap_` and
operator-supplied slot mapping, not a private reference inventory. An operator can
retain an existing dashboard namespace and ordered service inventory through the
settings described in [METRICS.md](METRICS.md), without changing collectors,
InfluxDB destinations, timestamps or sampling intervals.

Legacy first-release fan keys such as `stall_cycles` are intentionally not silently
accepted by the stricter PWM parser. Review existing configuration against the
shipped templates and validate it before any target maintenance. There is no
automatic configuration migration or physical deployment in source consolidation.

## Metrics compatibility checks

`tests/test_metrics_compatibility.py` AST-selects the actual settings, validators,
encoder, service collector, collection dispatcher, CLI loop and writer. It does
not import the daemon. Subprocess and HTTP effects are inert mocks that fail
unless explicitly configured; configuration is an isolated environment mapping.

The cases cover:

- unchanged generic settings/template and byte-exact default output for every
  measurement emitted by the collector;
- leading measurement-prefix replacement only, repeated suffixes, escaped
  measurement/tag/field content, timestamps and foreign namespaces;
- invalid prefix types/syntax and record-separator injection, rejecting the whole
  output batch before printing or constructing an HTTP write;
- CSV order/spelling, explicit suffixes and instances, whitespace rules, duplicate,
  empty, overlong, path, option, glob and command/line-injection rejection;
- configured inactive/disabled units still emitted with their original names and
  integer active/enabled fields, with unchanged systemctl argument structure;
- equal dry-run/HTTP bodies, unmodified input lists, unchanged URL/query/headers/
  timeout, empty output and missing credentials;
- the actual one-shot dry-run CLI path through every slow-tier collector, plus
  the existing service/status, SMART and EC/tier-gating regression assertions.

These are offline behavioral contracts, not evidence of target deployment,
InfluxDB acceptance, actual service state or physical sensor readings.
