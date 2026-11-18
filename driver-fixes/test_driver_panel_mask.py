#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Pinned panel callback in RAM only: mask correction is NOT deployment approval."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

BASE = "5a822199043a65298d486c26e1e3b5ab6b8e816d98013113bebd8f2c83f137d3"
NAME = "qnap8528_led_panel_brightness_set"

PRELUDE = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <errno.h>
typedef uint8_t u8;
#define LED_UNREGISTERING 1
struct led_classdev { unsigned flags; };
enum led_brightness { LED_OFF=0, LED_FULL=100 };
static u8 registers[0x247];
static int calls, fail_at, inject_at_read;
static int ops[8], addresses[8], values[8];
#define CHECK(x) do { if (!(x)) { fprintf(stderr,"line %d: %s\n",__LINE__,#x); exit(2); } } while(0)
/* Deliberately no hardware symbols, ports, filesystem IO or kernel transport. */
static int qnap8528_ec_read(int address, u8 *value) {
    CHECK(address>=0 && address<0x247);
    ops[calls]=0; addresses[calls]=address; ++calls;
    if(calls==fail_at) return -EIO;
    *value=registers[address];
    if(inject_at_read && address==0x245) {
        registers[address]^=0x01; inject_at_read=0;
    }
    return 0;
}
static int qnap8528_ec_write(int address, u8 value) {
    CHECK(address>=0 && address<0x247);
    ops[calls]=1; addresses[calls]=address; values[calls]=value; ++calls;
    if(calls==fail_at) return -EIO;
    registers[address]=value; return 0;
}
static void reset(int initial) {
    for(int i=0;i<0x247;i++) registers[i]=0;
    registers[0x243]=211; registers[0x246]=212; registers[0x245]=initial;
    calls=0; fail_at=0; inject_at_read=0;
}
'''

CASES = r'''
int main(int argc, char **argv) {
    CHECK(argc==2); int fixed=atoi(argv[1]);
    struct led_classdev device={0};
    unsigned vectors=0, unregister_vectors=0, failures=0;
    const int expected_ops[]={1,0,1,1,0,1};
    const int expected_addresses[]={0x243,0x245,0x245,0x246,0x245,0x245};
    for(int initial=0;initial<256;initial++) {
        for(int brightness=0;brightness<=100;brightness++) {
            reset(initial);
            CHECK(qnap8528_led_panel_brightness_set(&device,(enum led_brightness)brightness)==0);
            CHECK(calls==6);
            for(int i=0;i<6;i++) {
                CHECK(ops[i]==expected_ops[i]); CHECK(addresses[i]==expected_addresses[i]);
            }
            CHECK(values[0]==brightness && values[2]==(initial|0x10));
            CHECK(values[3]==brightness);
            int expected=fixed ? (initial & 0xef) : ((initial|0x10)|0xef);
            CHECK(registers[0x245]==expected);
            CHECK(registers[0x243]==brightness && registers[0x246]==brightness);
            ++vectors;
        }
    }
    device.flags=LED_UNREGISTERING;
    for(int brightness=0;brightness<=100;brightness++) {
        reset(0x5a);
        CHECK(qnap8528_led_panel_brightness_set(&device,(enum led_brightness)brightness)==0);
        CHECK(calls==0 && registers[0x245]==0x5a);
        CHECK(registers[0x243]==211 && registers[0x246]==212); ++unregister_vectors;
    }
    device.flags=0;
    for(int phase=1;phase<=6;phase++) {
        reset(0x02); fail_at=phase;
        CHECK(qnap8528_led_panel_brightness_set(&device,LED_FULL)==-EIO);
        CHECK(calls==phase); /* first error stops, no silent success or retry */
        CHECK(registers[0x243]==(phase>1 ? 100 : 211));
        CHECK(registers[0x246]==(phase>4 ? 100 : 212));
        CHECK(registers[0x245]==(phase>3 ? 0x12 : 0x02)); ++failures;
    }
    /* Reproduce an unresolved race, not claim the one-line fix serializes it. */
    reset(0x02); inject_at_read=1;
    CHECK(qnap8528_led_panel_brightness_set(&device,LED_FULL)==0);
    CHECK((values[2]&0x01)==0); /* stale read overwrote the concurrent toggle */
    CHECK((registers[0x245]&0x01)==(fixed ? 0 : 1));
    printf("{\"vectors\":%u,\"unregister_vectors\":%u,\"failure_phases\":%u,"
           "\"partial_failure_unresolved\":true,\"rmw_race_reproduced\":true,"
           "\"fixed_expectation\":%s,\"hardware_io\":false}\n",
           vectors,unregister_vectors,failures,fixed?"true":"false");
    return 0;
}
'''


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def extract(text):
    start = text.index("static int " + NAME + "(")
    opening = text.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--scratch-root", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    root = args.scratch_root.resolve(strict=True)
    runtime = os.environ.get("TMPDIR")
    if not runtime or root != Path(runtime).resolve(strict=True) or root == Path("/tmp") or Path("/tmp") in root.parents:
        parser.error("scratch must equal runtime TMPDIR, outside /tmp")
    raw = args.source.read_bytes()
    if args.source_sha256 != BASE or digest(raw) != BASE:
        parser.error("source must explicitly match pinned original driver")
    patch = Path(__file__).resolve().with_name("qnap8528-1.22-panel-mask.patch")
    receipts = []

    def run(command, cwd, expected=0):
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=30)
        receipts.append(dict(command=command, rc=result.returncode, stdout=result.stdout, stderr=result.stderr))
        if result.returncode != expected:
            raise RuntimeError(json.dumps(receipts[-1]))
        return result

    output: dict = dict(source_sha256=BASE, patch_sha256=digest(patch.read_bytes()),
                  harness_sha256=digest(Path(__file__).read_bytes()), variants={}, receipts=receipts,
                  deployment_approved=False, module_loaded=False, hardware_io=False)
    with tempfile.TemporaryDirectory(prefix="proxnap-panel-mask-", dir=root) as tmp:
        scratch = Path(tmp)
        (scratch / "src").mkdir()
        target = scratch / "src/qnap8528.c"
        target.write_bytes(raw)
        run(["git", "apply", "--check", str(patch)], scratch)
        run(["git", "apply", str(patch)], scratch)
        candidate = target.read_bytes()
        if candidate.count(b"\ttmp &= 0xef;") != 1:
            raise RuntimeError("candidate mask occurrence differs")
        if candidate.replace(b"\ttmp &= 0xef;", b"\ttmp |= 0xef;", 1) != raw:
            raise RuntimeError("candidate contains changes outside the one-line mask")
        output["candidate_sha256"] = digest(candidate)
        for label, text, flag in (("baseline", raw.decode(), "0"), ("mask_only", candidate.decode(), "1")):
            cfile, binary = scratch / (label + ".c"), scratch / label
            cfile.write_text(PRELUDE + extract(text) + CASES)
            run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-fsanitize=undefined",
                 "-fno-sanitize-recover=all", str(cfile), "-o", str(binary)], scratch)
            output["variants"][label] = json.loads(run([str(binary), flag], scratch).stdout)
        rejected = run([str(scratch / "baseline"), "1"], scratch, expected=2)
        if "registers[0x245]==expected" not in rejected.stderr:
            raise RuntimeError("original-source negative control failed for an unexpected reason")
        output["unfixed_negative_control_rejected"] = True
    output["scratch_removed"] = not Path(tmp).exists()
    if not output["scratch_removed"]:
        raise RuntimeError("owned temporary directory was not removed")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
