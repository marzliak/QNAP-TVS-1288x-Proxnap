# Metrics configuration and compatibility

The optional collector writes InfluxDB 2.x line protocol directly. Configure its
installed environment file using `configs/metrics.env.example`; keep credentials
and machine-specific inventory outside this public source tree.

## Measurement namespace

`PROXNAP_METRICS_MEASUREMENT_PREFIX` defaults to `proxnap`. Values must match the
ASCII pattern `[A-Za-z][A-Za-z0-9_]*`: no empty values, whitespace, delimiters,
backslashes or record separators. Invalid settings raise `ValueError` before
collection begins; the writer also rejects embedded CR/LF in encoded points.

For example, an operator with an existing `compat_cpu` dashboard can set:

```ini
PROXNAP_METRICS_MEASUREMENT_PREFIX=compat
```

Only the initial `proxnap_` of a measurement is replaced. `proxnap_cpu` becomes
`compat_cpu`; `proxnap_proxnap_status` becomes `compat_proxnap_status`. The suffix,
escaped measurement content, tags (including `host` and `service`), field names,
field values and timestamp remain byte-exact. Other measurement namespaces are
untouched. The default is a byte-for-byte no-op on valid encoded points.

Collectors and the `line`/`emit` APIs continue to produce generic names. The pure
`transform_measurement_prefix(encoded, prefix)` helper is applied at the common
`write_influx` boundary, before either dry-run printing or HTTP payload creation.
There is no process-wide mutable namespace and no dashboard rewrite or historical
data migration. Select the existing namespace **before** changing an installation
if continuity is required; a different namespace creates different series.

## Service inventory

`PROXNAP_METRICS_SERVICES` is an ordered, nonempty CSV of literal systemd units.
If unset, it retains the exact generic inventory shipped in the example template.
For example:

```ini
PROXNAP_METRICS_SERVICES=example-agent,example-worker@east.service,example.timer
```

Optional ASCII spaces around CSV entries are removed. Otherwise each identifier
is retained exactly as configured: a bare name is **not** rewritten with a
`.service` suffix in the emitted `service` tag. Do not change a historical bare
name to a suffixed name unless a distinct series is intended.

The supported conservative syntax starts with an ASCII letter, digit or
underscore and continues with ASCII letters, digits, `_`, `.`, `:`, `@` or `-`.
At most one `@` is allowed and it must not end the identifier. Names containing a
dot must end with a recognized systemd suffix: `service`, `socket`, `target`,
`device`, `mount`, `automount`, `swap`, `timer`, `path`, `slice` or `scope`. The
complete unit name must fit 255 characters, including the implicit `.service`
for a bare name. Empty entries, duplicates, options, paths, globs, backslash
escapes, shell metacharacters, tabs, CR/LF and non-ASCII names are rejected with
`ValueError`; this setting is not a shell command or unit-pattern expression.

Every configured unit is queried using the existing `systemctl is-active` and
`is-enabled` calls at the unchanged fast-tier cadence. Inactive or disabled units
are **not** filtered out: their series retain `active=0i` and/or `enabled=0i`.
The shared status series remains collected after the service inventory. This
configuration observes services; it does not enable, disable or restart them.

## Other settings are unchanged

The InfluxDB URL, organization, bucket, token, precision and timeout keep their
existing environment variables and behavior. Fast/medium/EC/slow collection
intervals are unchanged. `PROXNAP_METRICS_SLOT_MAP` still accepts an inline JSON
object mapping operator-supplied disk identities to slot labels. No machine map
or deployment-specific service list is shipped.

Offline acceptance and its physical/runtime limits are recorded in
[VALIDATION.md](VALIDATION.md#metrics-compatibility-checks).
