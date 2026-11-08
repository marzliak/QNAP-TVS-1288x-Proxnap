#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Range-only alternative: pinned real callback/transport, RAM-only C fixture.

Reuse the reviewed adjacent C/RAM fixture without changing its source. Separately
parameterize negative-input validation and compound-lock expectations. Neither
baseline nor this alternative has the compound lock. No device IO or module load.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

BASE = "5a822199043a65298d486c26e1e3b5ab6b8e816d98013113bebd8f2c83f137d3"
PATCH = "4418fe38d6d7c02ad9b6a148117382fe840b1ece8c28fa0ec903b06bb50226b8"
CANDIDATE = "802b03d613726a638ec5bcfc285a92388e38a32e715109ce0ee1978f02ca8657"
FIXTURE = "504526c0fa1fddbc8837e3bbbbbd8354ab0f40c7b8264b2c8e330ef7634e86e0"

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--scratch-root", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    root = args.scratch_root.resolve(strict=True)
    runtime = os.environ.get("TMPDIR")
    if not runtime or root != Path(runtime).resolve(strict=True) or root == Path("/tmp") or Path("/tmp") in root.parents:
        parser.error("scratch must equal runtime TMPDIR, outside /tmp")
    raw = args.source.read_bytes()
    if args.source_sha256 != BASE or sha(raw) != BASE:
        parser.error("source must explicitly match pinned captured bytes")
    here = Path(__file__).resolve().parent
    patch = here / "qnap8528-1.22-pwm-range-validation.patch"
    fixture = here / "test_driver_fans.py"
    fixture_raw = fixture.read_bytes()
    if sha(patch.read_bytes()) != PATCH or sha(fixture_raw) != FIXTURE:
        parser.error("patch or reviewed RAM fixture fingerprint differs")
    # This is a hash-pinned test-only library, never a production daemon import.
    lib: dict[str, Any] = {"__name__": "ram_fixture_library", "__file__": str(fixture)}
    exec(compile(fixture_raw, str(fixture), "exec"), lib)
    extract = lib["extract_function"]
    main_c = lib["CASES"]
    edits = [
        ("int patched, i, raw", "int patched, range_fixed, i, raw"),
        ("CHECK(argc==2); patched=atoi(argv[1]);", "CHECK(argc==3); patched=atoi(argv[1]); range_fixed=atoi(argv[2]);"),
        ("if (patched) CHECK(ret==-ERANGE", "if (range_fixed) CHECK(ret==-ERANGE"),
    ]
    for old, new in edits:
        assert main_c.count(old) == 1
        main_c = main_c.replace(old, new, 1)
    receipts: list[dict[str, Any]] = []
    def run(argv, cwd, expected=0):
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=30,
                                env={**os.environ, "TMPDIR": str(cwd), "LC_ALL": "C"})
        receipts.append(dict(command=argv, rc=result.returncode, stdout=result.stdout, stderr=result.stderr))
        if expected == 0 and result.returncode != 0:
            raise RuntimeError(json.dumps(receipts[-1]))
        if expected != 0 and result.returncode == 0:
            raise AssertionError("unfixed-source negative control unexpectedly passed")
        return result
    result: dict[str, Any] = dict(source_sha256=BASE, patch_sha256=PATCH, candidate_sha256=CANDIDATE,
                  reused_fixture_sha256=FIXTURE, variants={}, receipts=receipts,
                  compound_lock_expected=False, hardware_io=False, module_loaded=False)
    with tempfile.TemporaryDirectory(prefix="proxnap-driver-pwm-range-", dir=root) as tmp:
        scratch = Path(tmp)
        (scratch / "src").mkdir()
        target = scratch / "src/qnap8528.c"
        target.write_bytes(raw)
        run(["git", "apply", "--check", str(patch)], scratch)
        run(["git", "apply", str(patch)], scratch)
        assert sha(target.read_bytes()) == CANDIDATE
        base, candidate = raw.decode(), target.read_text()
        callback = "qnap8528_hwmon_write"
        assert candidate.replace(extract(candidate, callback), extract(base, callback), 1) == base
        names = ["qnap8528_ec_read", "qnap8528_ec_write", "qnap8528_fan_rpm_get",
                 "qnap8528_fan_pwm_get", "qnap8528_fan_pwm_set", callback]
        for name in names[:-1]:
            assert extract(base, name) == extract(candidate, name)
        for label, text, flag in (("baseline", base, "0"), ("range_only", candidate, "1")):
            cfile, binary = scratch / (label + ".c"), scratch / label
            cfile.write_text(lib["PRELUDE"] + "\n\n".join(extract(text, name) for name in names) + main_c)
            run(["gcc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wno-error=type-limits",
                 "-Wno-unused-parameter", "-fsanitize=undefined", "-fno-sanitize-recover=all",
                 str(cfile), "-o", str(binary)], scratch)
            row = json.loads(run([str(binary), "0", flag], scratch).stdout)
            assert row["pwm_vectors"] == 5120 and row["rpm_maps"] == 22 and not row["patched"]
            row["range_validation_expected"] = flag == "1"
            result["variants"][label] = row
        negative = run([str(scratch / "baseline"), "0", "1"], scratch, expected=1)
        assert "ret==-ERANGE" in negative.stderr
        result["unfixed_negative_control_rejected"] = True
    result["scratch_path"] = tmp
    result["scratch_removed"] = not Path(tmp).exists()
    assert result["scratch_removed"]
    result["harness_sha256"] = sha(Path(__file__).read_bytes())
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
