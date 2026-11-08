# Agent instructions — Proxnap portable source

This repository owns the reusable implementation, generic configuration templates,
units, tests and documentation. Deployment-specific records and configuration
belong to the operator's infrastructure repository, not a second edited code copy.
Never copy private operational notebooks, host credentials, firmware captures or
historical private Git objects into this public repository.

## Change and publication boundaries

- Work and integrate on `homolog`; do not promote `main`, tags or releases without
  an explicit owner request. Publication is not deployment.
- Preserve this repository's existing `src/proxnap/` organization and installed CLI
  compatibility. Avoid parallel legacy implementation trees; thin launchers may
  preserve earlier command names while importing one implementation.
- Root userspace code is MIT. Files with GPL SPDX headers and `driver-fixes/` retain
  their specific license; do not relicense third-party/EC-derived code as MIT.
- Keep tests, safety documentation, installer, units and configuration semantics
  aligned. Report mocks/offline tests separately from physical validation.

## Hardware safety

- Never exercise real EC/sysfs/serial/input/port writes, reboot, shutdown, package
  upgrades, module reloads or service changes as part of development tests.
- Tests of final actions require mandatory inert mocks and scratch-confined IO;
  production daemons are not safe fixtures merely because main() is not called.
- Preserve CPU50%/rear40% minimum policy, temporary stop/protection100%, missing
  sensor handling and immediate thermal protection. Do not lower floors or cap CPU
  performance to hide a thermal problem.
- Shared-device locking must cover complete transactions and error/cleanup paths.
  Unknown/stale/malformed data must not become healthy status.
- The optional compound EC driver patch is experimental: passing host-exclusion
  models does not prove acceptable physical latency, autonomous tach latching,
  semantic ACK or rollback. It must not be installed automatically.

## Validation and disclosure

Run the documented complete affected suite using bytecode-disabled Python and a
private runtime TMPDIR. Native driver tests require explicitly supplied pinned
source and RAM-only transport; never run the raw EC helper alongside a real driver.
No validation should create virtualenvs, caches, logs or captured hardware inputs
inside the published tree. Preserve meaningful assertions and negative controls.

Before public push inspect all new files, changed lines, patch contexts, commit
metadata and reachable new objects. Keep private IPs/hostnames/MACs/serials/accounts,
private paths, tokens and evidence out. Use explicit placeholders in examples.
Retain the clean public history; do not merge private repository ancestry.
