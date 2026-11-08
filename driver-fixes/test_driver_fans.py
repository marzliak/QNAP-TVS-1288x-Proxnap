#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise exact qnap8528 C functions with RAM-only EC and lock instrumentation.

Requires an explicit, hash-pinned external source; no download, module load,
actual kernel callback, sysfs or port access. Scratch is removed on completion.
The patch itself is applied by git inside the isolated scratch directory.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

BASE_SHA256 = '5a822199043a65298d486c26e1e3b5ab6b8e816d98013113bebd8f2c83f137d3'


def extract_function(text, name):
    match = re.search(r'^static int ' + re.escape(name) + r'\([^;]*?\)\s*\{', text, re.M | re.S)
    if match is None:
        raise ValueError('Missing C function: ' + name)
    depth = 0
    for offset in range(text.index('{', match.start()), len(text)):
        if text[offset] == '{':
            depth += 1
        elif text[offset] == '}':
            depth -= 1
            if depth == 0:
                return text[match.start():offset + 1]
    raise ValueError('Unclosed C function: ' + name)


PRELUDE = r'''
#include <assert.h>
#include <errno.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
struct device { int unused; };
enum hwmon_sensor_types { hwmon_pwm, hwmon_temp };
#ifndef ENOTSUPP
#define ENOTSUPP 524
#endif
#define QNAP8528_EC_DAT_PORT 0x68
static int held, locks, unlocks, delays, commands, writes, current, checks;
static int fail_send, fail_clear, fail_obf, fail_ibf, clear_calls, obf_calls, ibf_calls;
static int inject, injected, auto_tach, qnap8528_ec_lock;
static u8 ram[0x1000];
static u16 command_log[64];
#define CHECK(expr) do { checks++; if (!(expr)) { fprintf(stderr, "check %d line %d: %s\n",checks,__LINE__,#expr); exit(1); } } while (0)
static int qnap8528_ec_read(u16 command, u8 *data);
static void mutex_lock(int *lock) { (void)lock; CHECK(!held); held=1; locks++; }
static void mutex_unlock(int *lock) {
    u8 ignored;
    (void)lock; CHECK(held); held=0; unlocks++;
    if (inject && commands==1) {
        inject=0; injected=1;
        CHECK(qnap8528_ec_read(0x143,&ignored)==0);
    }
}
#define lockdep_assert_held(lock) do { (void)(lock); CHECK(held); } while (0)
static int qnap8528_ec_clear_obf(void) { CHECK(held); return ++clear_calls==fail_clear ? -EBUSY : 0; }
static int qnap8528_ec_wait_obf_set(void) { CHECK(held); return ++obf_calls==fail_obf ? -EBUSY : 0; }
static int qnap8528_ec_wait_ibf_clear(void) { CHECK(held); return ++ibf_calls==fail_ibf ? -EBUSY : 0; }
static int qnap8528_ec_send_command(u16 command) {
    CHECK(held); CHECK(commands<64); command_log[commands++]=command;
    current=command & 0x7fff; CHECK(current<(int)sizeof(ram));
    return commands==fail_send ? -EBUSY : 0;
}
static void outb(u8 value, int port) {
    CHECK(held); CHECK(port==QNAP8528_EC_DAT_PORT); ram[current]=value; writes++;
}
static u8 inb(int port) {
    u8 value;
    CHECK(held); CHECK(port==QNAP8528_EC_DAT_PORT); value=ram[current];
    if (auto_tach && current==0x620) { ram[0x620]=8; ram[0x621]=0; auto_tach=0; }
    return value;
}
static void udelay(int us) { CHECK(held); CHECK(us==50); delays++; }
static void reset(void) {
    CHECK(!held); memset(ram,0,sizeof(ram)); memset(command_log,0,sizeof(command_log));
    locks=unlocks=delays=commands=writes=0;
    fail_send=fail_clear=fail_obf=fail_ibf=clear_calls=obf_calls=ibf_calls=0;
    inject=injected=auto_tach=0;
}
'''

CASES = r'''
int main(int argc,char **argv) {
    int patched, i, raw, ret, before, pwm_vectors=0, rpm_maps=0;
    const int fans[]={0,1,2,3,4,5,6,7,20,21,22,23,24,25,30,31,32,33,34,35};
    const long negative[]={-1,-128,-255,-256,-257,LONG_MIN};
    const long upper[]={256,65536,LONG_MAX};
    CHECK(argc==2); patched=atoi(argv[1]);
    /* Every valid raw input on every supported PWM channel retains its map/scale. */
    for (i=0;i<(int)(sizeof(fans)/sizeof(fans[0]));i++) {
        int fan=fans[i], mode=fan<6?0x220:fan<8?0x223:fan<26?0x221:0x222;
        int duty=fan<6?0x22e:fan<8?0x24b:fan<26?0x22f:0x23b;
        for (raw=0;raw<=255;raw++) {
            pwm_vectors++;
            reset(); CHECK(qnap8528_hwmon_write(NULL,hwmon_pwm,0,fan,raw)==0);
            CHECK(commands==2 && writes==2 && delays==2 && locks==unlocks);
            CHECK(locks==(patched?1:2)); CHECK(ram[mode]==0x10);
            CHECK(ram[duty]==raw*100/255);
            CHECK(command_log[0]==(mode|0x8000) && command_log[1]==(duty|0x8000));
            CHECK(qnap8528_fan_pwm_get(fan)==(raw*100/255)*255/100);
            CHECK(!held && locks==unlocks);
        }
    }
    /* Reproduce the old invalid-input wrap; candidate must reject before any IO. */
    for (i=0;i<(int)(sizeof(negative)/sizeof(negative[0]));i++) {
        reset(); ret=qnap8528_hwmon_write(NULL,hwmon_pwm,0,6,negative[i]);
        if (patched) CHECK(ret==-ERANGE && !commands && !writes && !locks);
        else CHECK(ret==0 && writes==2 && ram[0x24b]==((u8)negative[i])*100/255);
    }
    for (i=0;i<(int)(sizeof(upper)/sizeof(upper[0]));i++) {
        reset(); CHECK(qnap8528_hwmon_write(NULL,hwmon_pwm,0,6,upper[i])==0);
        CHECK(ram[0x24b]==100);
    }
    reset(); CHECK(qnap8528_hwmon_write(NULL,hwmon_temp,0,6,100)==-ENOTSUPP);
    CHECK(!commands && !locks);
    reset(); CHECK(qnap8528_fan_pwm_set(99,255)==-EINVAL && !commands && !locks);
    CHECK(qnap8528_fan_rpm_get(99)==-EINVAL && !commands && !locks);
    /* Force a valid foreign host read at the first release of the actual lock. */
    reset(); inject=1; CHECK(qnap8528_fan_pwm_set(6,128)==0);
    if (patched) { CHECK(!injected && commands==2); CHECK(qnap8528_ec_read(0x143,&ram[1])==0); }
    else CHECK(injected && commands==3);
    CHECK(command_log[0]==0x8223);
    CHECK(command_log[1]==(patched?0x824b:0x143));
    CHECK(command_log[2]==(patched?0x143:0x824b));
    reset(); ram[0x620]=7;ram[0x621]=0xff;inject=1;
    CHECK(qnap8528_fan_rpm_get(6)==0x07ff);
    if (patched) { CHECK(!injected && commands==2); CHECK(qnap8528_ec_read(0x143,&ram[1])==0); }
    else CHECK(injected && commands==3);
    CHECK(command_log[0]==0x620);
    CHECK(command_log[1]==(patched?0x621:0x143));
    CHECK(command_log[2]==(patched?0x143:0x621));
    /* Partial mode+duty failure is propagated, not claimed to be rolled back. */
    reset(); ram[0x223]=0x80;ram[0x24b]=40;fail_send=2;
    CHECK(qnap8528_fan_pwm_set(6,255)==-EBUSY);
    CHECK(ram[0x223]==0x10 && ram[0x24b]==40 && writes==1 && delays==2);
    CHECK(!held && locks==unlocks);
    reset(); fail_send=1;CHECK(qnap8528_fan_pwm_set(6,255)==-EBUSY);
    CHECK(commands==1 && !writes && delays==1 && !held && locks==unlocks);
    reset();fail_ibf=1;CHECK(qnap8528_fan_pwm_set(6,255)==-EBUSY);
    CHECK(commands==1 && !writes && delays==1 && !held && locks==unlocks);
    for (i=1;i<=2;i++) {
        reset();fail_send=i;CHECK(qnap8528_fan_rpm_get(6)==-EBUSY);
        CHECK(!held && locks==unlocks && commands==i && delays==i);
        reset();fail_clear=i;CHECK(qnap8528_fan_rpm_get(6)==-EBUSY);
        CHECK(!held && locks==unlocks && commands==i-1 && delays==i);
        reset();fail_obf=i;CHECK(qnap8528_fan_rpm_get(6)==-EBUSY);
        CHECK(!held && locks==unlocks && commands==i && delays==i);
    }
    /* A host lock does NOT latch autonomous firmware tach updates. */
    reset();ram[0x620]=7;ram[0x621]=0xff;auto_tach=1;
    CHECK(qnap8528_fan_rpm_get(6)==0x0700);CHECK(!held && locks==unlocks);
    /* Every advertised RPM register pair, including swapped auxiliary pairs. */
    for (i=0;i<22;i++) {
        rpm_maps++;
        int fan=i<20?fans[i]:i==20?10:11;
        int hi,lo;
        if(fan<6){hi=(fan+0x312)*2;lo=fan*2+0x625;}
        else if(fan<8){hi=(fan+0x30a)*2;lo=(fan-6)*2+0x621;}
        else if(fan==10){hi=0x65b;lo=0x65a;}
        else if(fan==11){hi=0x65e;lo=0x65d;}
        else if(fan<26){hi=(fan+0x30e)*2;lo=(fan-20)*2+0x645;}
        else{hi=(fan+0x2f8)*2;lo=(fan-30)*2+0x62d;}
        reset();ram[hi]=0x12;ram[lo]=0x34;CHECK(qnap8528_fan_rpm_get(fan)==0x1234);
        CHECK(command_log[0]==hi && command_log[1]==lo && delays==2 && !held);
    }
    /* Public wrappers remain usable by non-fan clients, including both errors. */
    reset();CHECK(qnap8528_ec_write(0x155,3)==0 && ram[0x155]==3);
    before=commands;CHECK(qnap8528_ec_read(0x155,&ram[1])==0 && ram[1]==3);
    CHECK(commands==before+1 && locks==unlocks && !held);
    printf("{\"patched\":%s,\"checks\":%d,\"pwm_vectors\":%d,\"rpm_maps\":%d,\"autonomous_tach_not_fixed\":true}\n",patched?"true":"false",checks,pwm_vectors,rpm_maps);
    return 0;
}
'''


def run_checked(command, cwd, timeout=15):
    process = subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    if process.returncode:
        raise RuntimeError(json.dumps({'command': command, 'rc': process.returncode,
                                      'stdout': process.stdout, 'stderr': process.stderr}))
    return process.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    args = parser.parse_args()
    source_bytes = args.source.read_bytes()
    actual = hashlib.sha256(source_bytes).hexdigest()
    if actual != BASE_SHA256:
        parser.error('source fingerprint differs; this patch requires the exact captured 1.22 source')
    source = source_bytes.decode('utf-8')
    here = Path(__file__).resolve().parent
    patch = here / 'qnap8528-1.22-fan-host-serialization.patch'
    if not os.environ.get('TMPDIR'):
        parser.error('TMPDIR must explicitly identify authorized session scratch')
    results = {}
    with tempfile.TemporaryDirectory(prefix='proxnap-fan-driver-', dir=os.environ['TMPDIR']) as temp:
        scratch = Path(temp)
        (scratch / 'src').mkdir()
        target = scratch / 'src/qnap8528.c'
        target.write_text(source)
        run_checked(['git', 'apply', '--check', str(patch)], scratch)
        run_checked(['git', 'apply', str(patch)], scratch)
        candidate = target.read_text()
        # Preserve unrelated functions and exactly the transport operations/delay.
        for name in ('qnap8528_fan_pwm_get', 'qnap8528_ec_wait_ibf_clear',
                     'qnap8528_ec_clear_obf', 'qnap8528_ec_wait_obf_set',
                     'qnap8528_ec_send_command'):
            assert extract_function(source, name) == extract_function(candidate, name), name
        for public, private in [('qnap8528_ec_read', '__qnap8528_ec_read_locked'),
                                ('qnap8528_ec_write', '__qnap8528_ec_write_locked')]:
            old = extract_function(source, public)
            expected = old.replace(public + '(', private + '(', 1)
            expected = expected.replace('mutex_lock(&qnap8528_ec_lock);',
                                        'lockdep_assert_held(&qnap8528_ec_lock);', 1)
            expected = expected.replace('\n\tmutex_unlock(&qnap8528_ec_lock);', '', 1)
            assert extract_function(candidate, private) == expected, private
        for label, text in [('baseline', source), ('candidate', candidate)]:
            names = []
            if label == 'candidate':
                names.append('__qnap8528_ec_read_locked')
            names.append('qnap8528_ec_read')
            if label == 'candidate':
                names.append('__qnap8528_ec_write_locked')
            names.extend(['qnap8528_ec_write', 'qnap8528_fan_rpm_get',
                          'qnap8528_fan_pwm_get', 'qnap8528_fan_pwm_set', 'qnap8528_hwmon_write'])
            cfile = scratch / (label + '.c')
            cfile.write_text(PRELUDE + '\n\n'.join(extract_function(text, name) for name in names) + CASES)
            flags = ['gcc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-Wno-type-limits',
                     '-Wno-unused-parameter', '-fsanitize=undefined', '-fno-sanitize-recover=all']
            run_checked(flags + [str(cfile), '-o', str(scratch / label)], scratch)
            output = run_checked([str(scratch / label), '1' if label == 'candidate' else '0'], scratch)
            results[label] = json.loads(output)
        results['base_sha256'] = actual
        results['candidate_sha256'] = hashlib.sha256(candidate.encode()).hexdigest()
        results['patch_sha256'] = hashlib.sha256(patch.read_bytes()).hexdigest()
        results['harness_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        results['kernel_module_built_or_loaded'] = False
        results['scratch_removed_on_exit'] = True
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
