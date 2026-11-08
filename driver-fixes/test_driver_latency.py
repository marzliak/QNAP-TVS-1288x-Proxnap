#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Literal captured C waits/transport and fan pairs, with mandatory RAM mocks.

Offline only. No kernel headers, port IO, sleeps, modules or device files.
Requires explicit pinned source hash and runtime scratch. Emits JSON receipts;
all generated sources/binaries disappear with the owned TemporaryDirectory.
Logical udelay budgets exclude execution overhead, scheduling and lock contention.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from typing import Any

SOURCE_SHA256 = '5a822199043a65298d486c26e1e3b5ab6b8e816d98013113bebd8f2c83f137d3'
HEADER_SHA256 = '1dac171f80378a72b3458fd5ce31b24a0bda2b30989175a8af68b31c0da7ed38'
PATCH_SHA256 = '8ce4347cc9320dc670f63c067f2978e166d5d9bf07200c89c50ffa1e74c411f7'
WAIT_NAMES = ['qnap8528_ec_wait_ibf_clear', 'qnap8528_ec_clear_obf',
              'qnap8528_ec_wait_obf_set']
FUNCTIONS = WAIT_NAMES + ['qnap8528_ec_send_command', 'qnap8528_ec_read',
                         'qnap8528_ec_write', 'qnap8528_fan_rpm_get',
                         'qnap8528_fan_pwm_set']
PHASES = {'ibf': [0], 'clear': [1], 'obf': [2], 'send': [0]*3,
          'read': [1, 0, 0, 0, 2], 'write': [0]*4,
          'rpm': [1, 0, 0, 0, 2]*2, 'pwm': [0]*8}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def extract(text, name):
    # Hash-pinned, reviewed source: these functions contain no brace literals.
    match = re.search(r'^static int ' + re.escape(name) + r'\([^;]*?\)\s*\{',
                      text, re.M | re.S)
    if not match:
        raise ValueError('missing function: ' + name)
    depth = 0
    for i in range(text.index('{', match.start()), len(text)):
        depth += (text[i] == '{') - (text[i] == '}')
        if depth == 0:
            return text[match.start():i+1]
    raise ValueError('unclosed function: ' + name)


PRELUDE = r'''
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint8_t u8;
typedef uint16_t u16;
#define BIT(n) (1U << (n))
static unsigned long long checks, clock_us;
#define CHECK(x) do { checks++; if (!(x)) { fprintf(stderr,"CHECK line %d: %s\n",__LINE__,#x); exit(90); } } while(0)
static int held, locks, unlocks, qnap8528_ec_lock, active=-1, phase_count;
static int failure, bad_before, direct_bad, op_is_direct;
static unsigned long long lock_start, lock_duration[16];
static u8 ram[4096];
static int wire_stage, command, out_count, read_count, write_count, tail_count;
struct phase { int kind, polls, delays, drains, lock_id; unsigned long long start, end; };
static struct phase phases[32];
struct output { int port, value, lock_id; unsigned long long at; };
static struct output outputs[32];
static void mutex_lock(int *p) {
    CHECK(p==&qnap8528_ec_lock); CHECK(!held); held=1; locks++;
    CHECK(locks<=16); lock_start=clock_us;
}
static void mutex_unlock(int *p) {
    CHECK(p==&qnap8528_ec_lock); CHECK(held); held=0;
    lock_duration[unlocks++]=clock_us-lock_start;
}
#define lockdep_assert_held(p) do { CHECK((p)==&qnap8528_ec_lock); CHECK(held); } while(0)
static int kind_of(const char *name) {
    if (!strcmp(name,"qnap8528_ec_wait_ibf_clear")) return 0;
    if (!strcmp(name,"qnap8528_ec_clear_obf")) return 1;
    if (!strcmp(name,"qnap8528_ec_wait_obf_set")) return 2;
    CHECK(0); return -1;
}
static u8 mock_inb(int port, const char *caller) {
    CHECK(held);
    if (port==QNAP8528_EC_CMD_PORT) {
        int k=kind_of(caller), bad, ready;
        struct phase *p;
        if (active<0) {
            CHECK(phase_count<32); active=phase_count++;
            phases[active].kind=k; phases[active].start=clock_us;
            phases[active].lock_id=locks;
        }
        p=&phases[active]; CHECK(p->kind==k); p->polls++;
        bad=op_is_direct ? direct_bad : (active==failure ? -1 : bad_before);
        ready=bad>=0 && p->polls>bad;
        p->end=clock_us;
        if (ready) active=-1;
        return k==0 ? (ready?0:BIT(1)) : k==1 ? (ready?0:BIT(0)) : (ready?BIT(0):0);
    }
    CHECK(port==QNAP8528_EC_DAT_PORT);
    if (!strcmp(caller,"qnap8528_ec_clear_obf")) {
        CHECK(active>=0); phases[active].drains++; return 0x5a;
    }
    CHECK(wire_stage==3); CHECK(command>=0 && command<4096);
    read_count++; wire_stage=0; return ram[command];
}
static void mock_outb(u8 value, int port) {
    CHECK(held); CHECK(out_count<32);
    outputs[out_count++]=(struct output){port,value,locks,clock_us};
    if (port==QNAP8528_EC_CMD_PORT) {
        CHECK(value==0x88); wire_stage=1; command=0; return;
    }
    CHECK(port==QNAP8528_EC_DAT_PORT);
    if (wire_stage==1) { command=value<<8; wire_stage=2; }
    else if (wire_stage==2) { command|=value; wire_stage=3; }
    else {
        CHECK(wire_stage==3); CHECK(command&0x8000);
        CHECK((command&0x7fff)<4096); ram[command&0x7fff]=value;
        write_count++; wire_stage=0;
    }
}
static void mock_udelay(int us) {
    CHECK(held); CHECK(us==QNAP8528_EC_UDELAY || us==50);
    if (us==QNAP8528_EC_UDELAY) {
        CHECK(active>=0); phases[active].delays++;
        clock_us+=(unsigned)us; phases[active].end=clock_us;
    } else { tail_count++; clock_us+=50; active=-1; }
}
#define inb(port) mock_inb((port),__func__)
#define outb(value,port) mock_outb((value),(port))
#define udelay(us) mock_udelay((us))
'''

MAIN = r'''
int main(int argc,char **argv) {
    int ret, direct; u8 data=0xa5;
    CHECK(argc==5); failure=atoi(argv[2]); bad_before=atoi(argv[3]); direct_bad=atoi(argv[4]);
    direct=!strcmp(argv[1],"ibf") || !strcmp(argv[1],"clear") || !strcmp(argv[1],"obf");
    op_is_direct=direct;
    ram[0x620]=0x12;ram[0x621]=0x34;ram[0x155]=0x56;
    ram[0x223]=0x80;ram[0x24b]=40;
    if(direct || !strcmp(argv[1],"send")) mutex_lock(&qnap8528_ec_lock);
    if(!strcmp(argv[1],"ibf")) ret=qnap8528_ec_wait_ibf_clear();
    else if(!strcmp(argv[1],"clear")) ret=qnap8528_ec_clear_obf();
    else if(!strcmp(argv[1],"obf")) ret=qnap8528_ec_wait_obf_set();
    else if(!strcmp(argv[1],"send")) ret=qnap8528_ec_send_command(0x155);
    else if(!strcmp(argv[1],"read")) ret=qnap8528_ec_read(0x155,&data);
    else if(!strcmp(argv[1],"write")) ret=qnap8528_ec_write(0x155,0x67);
    else if(!strcmp(argv[1],"rpm")) ret=qnap8528_fan_rpm_get(6);
    else if(!strcmp(argv[1],"pwm")) ret=qnap8528_fan_pwm_set(6,255);
    else { CHECK(0); return 90; }
    if(direct || !strcmp(argv[1],"send")) mutex_unlock(&qnap8528_ec_lock);
    CHECK(!held); CHECK(locks==unlocks);
    printf("{\"rc\":%d,\"ebusy\":%d,\"data\":%u,\"ram155\":%u,\"mode\":%u,\"duty\":%u,\"reads\":%d,\"writes\":%d,\"tails\":%d,\"clock_us\":%llu,\"locks\":%d,\"unlocks\":%d,\"held\":%d,\"c_assertions\":%llu,\"phases\":[",
      ret,-EBUSY,data,ram[0x155],ram[0x223],ram[0x24b],read_count,write_count,tail_count,clock_us,locks,unlocks,held,checks);
    for(int i=0;i<phase_count;i++) {
      struct phase *p=&phases[i];
      printf("%s{\"kind\":%d,\"polls\":%d,\"delays\":%d,\"drains\":%d,\"lock_id\":%d,\"start_us\":%llu,\"end_us\":%llu}",i?",":"",p->kind,p->polls,p->delays,p->drains,p->lock_id,p->start,p->end);
    }
    printf("],\"outputs\":[");
    for(int i=0;i<out_count;i++) {
      struct output *p=&outputs[i];
      printf("%s{\"port\":%d,\"value\":%d,\"lock_id\":%d,\"at_us\":%llu}",i?",":"",p->port,p->value,p->lock_id,p->at);
    }
    printf("],\"lock_duration_us\":[");
    for(int i=0;i<unlocks;i++) printf("%s%llu",i?",":"",lock_duration[i]);
    puts("]}"); return 0;
}
'''


def cases():
    for op in ('ibf', 'clear', 'obf'):
        for bad in (0, 1, 4999, 5000, 5001, 5002, -1):
            yield dict(op=op, failure=-1, prior_bad=0, direct_bad=bad,
                       name=f'{op}:bad={bad}')
    for op in ('send', 'read', 'write', 'rpm', 'pwm'):
        for bad in (0, 1, 5000):
            yield dict(op=op, failure=-1, prior_bad=bad, direct_bad=0,
                       name=f'{op}:success:bad={bad}')
        for bad in (0, 5000):
            for phase in range(len(PHASES[op])):
                yield dict(op=op, failure=phase, prior_bad=bad, direct_bad=0,
                           name=f'{op}:timeout={phase}:prior_bad={bad}')


class Oracle:
    def __init__(self):
        self.assertions = 0

    def equal(self, observed, expected, context):
        self.assertions += 1
        if observed != expected:
            raise AssertionError(f'{context}: {observed!r} != {expected!r}')

    def verify(self, case, result, candidate):
        """Independent phase/byte schedule; never replaces executed transport C."""
        op = case['op']
        direct = op in ('ibf', 'clear', 'obf')
        fault = case['failure']
        if direct:
            bad = case['direct_bad']
            fault = 0 if bad < 0 or bad > 5000 else -1
        failed = fault >= 0
        count = fault+1 if failed else len(PHASES[op])
        observed = result['phases']
        self.equal(len(observed), count, case['name']+' first-error stop')
        pair = op in ('rpm', 'pwm')
        width = 5 if op in ('read', 'rpm') else 4
        tails = 0 if direct or op == 'send' else (2 if pair and count > width else 1)
        lock_count = tails if pair and not candidate else 1
        elapsed = 0
        expected_outputs = []
        duration = [0]*lock_count
        # Byte events occur only after the preceding successful wait.
        schedule = {}
        if not direct:
            commands = [0x620, 0x621] if op == 'rpm' else [0x8223, 0x824b] if op == 'pwm' else [0x8155] if op == 'write' else [0x155]
            for j, cmd in enumerate(commands):
                start = j*width if pair else 0
                if op in ('read', 'rpm'):
                    start += 1  # clear OBF before the three command IBF waits
                for delta, port, value in ((0, 0x6c, 0x88), (1, 0x68, cmd >> 8), (2, 0x68, cmd & 255)):
                    schedule[start+delta] = (port, value)
                if op in ('write', 'pwm'):
                    schedule[start+3] = (0x68, (0x10 if j == 0 else 100) if pair else 0x67)
        for i, p in enumerate(observed):
            bad = case['direct_bad'] if direct else case['prior_bad']
            timeout = failed and i == fault
            delays = 5001 if timeout else bad
            polls = 5001 if timeout else bad+1
            lock_id = i//width+1 if pair and not candidate else 1
            self.equal(p['kind'], PHASES[op][i], 'phase kind')
            self.equal(p['polls'], polls, 'literal loop polls')
            self.equal(p['delays'], delays, 'logical udelay calls')
            self.equal(p['drains'], delays if p['kind'] == 1 else 0, 'clear OBF data drains')
            self.equal(p['lock_id'], lock_id, 'phase mutex owner')
            self.equal(p['start_us'], elapsed, 'phase start logical timestamp')
            elapsed += delays*1000
            duration[lock_id-1] += delays*1000
            self.equal(p['end_us'], elapsed, 'phase end logical timestamp')
            if not timeout and i in schedule:
                port, value = schedule[i]
                expected_outputs.append(dict(port=port, value=value, lock_id=lock_id, at_us=elapsed))
            if tails and (timeout or (i+1) % width == 0):
                elapsed += 50
                duration[lock_id-1] += 50
        self.equal(result['outputs'], expected_outputs, 'exact ordered command/data bytes')
        self.equal(result['rc'], result['ebusy'] if failed else 0x1234 if op == 'rpm' else 0, 'first error return')
        self.equal(result['ebusy'], -16, 'captured errno EBUSY')
        self.equal(result['tails'], tails, '50us epilogues including error')
        self.equal(result['clock_us'], elapsed, 'logical total budget')
        self.equal(result['lock_duration_us'], duration, 'per-acquisition hold budget')
        self.equal(result['locks'], lock_count, 'lock acquisitions')
        self.equal(result['unlocks'], lock_count, 'all acquired locks released')
        self.equal(result['held'], 0, 'no leaked mutex')
        reads = (count//5 if op == 'rpm' else 1 if op == 'read' and not failed else 0)
        if op == 'rpm' and failed and count % 5 == 0:
            reads -= 1
        writes = (count//4 if op == 'pwm' else 1 if op == 'write' and not failed else 0)
        if op == 'pwm' and failed and count % 4 == 0:
            writes -= 1
        self.equal(result['reads'], reads, 'successful payload reads only')
        self.equal(result['writes'], writes, 'successful payload writes only')
        self.equal(result['data'], 0x56 if op == 'read' and not failed else 0xa5, 'read destination preserved on failure')
        self.equal(result['ram155'], 0x67 if op == 'write' and not failed else 0x56, 'failed write leaves old byte')
        self.equal(result['mode'], 0x10 if op == 'pwm' and writes >= 1 else 0x80, 'mode partial commit, no rollback')
        self.equal(result['duty'], 100 if op == 'pwm' and writes == 2 else 40, 'duty partial commit')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--source-sha256', required=True)
    parser.add_argument('--header', required=True, type=Path)
    parser.add_argument('--scratch-root', required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    raw = args.source.read_bytes()
    header = args.header.read_bytes()
    if args.source_sha256 != SOURCE_SHA256 or digest(raw) != args.source_sha256:
        parser.error('source SHA256 must explicitly match pinned captured source')
    if digest(header) != HEADER_SHA256:
        parser.error('header SHA256 mismatch')
    scratch_root = args.scratch_root.resolve(strict=True)
    runtime = os.environ.get('TMPDIR')
    if not runtime or scratch_root != Path(runtime).resolve(strict=True) or scratch_root == Path('/tmp') or Path('/tmp') in scratch_root.parents:
        parser.error('scratch must be exactly runtime TMPDIR, outside /tmp')
    patch = Path(__file__).resolve().with_name('qnap8528-1.22-fan-host-serialization.patch')
    if digest(patch.read_bytes()) != PATCH_SHA256:
        parser.error('patch SHA256 mismatch')
    defines = []
    for name in ('QNAP8528_EC_CMD_PORT', 'QNAP8528_EC_DAT_PORT', 'QNAP8528_EC_UDELAY', 'QNAP8528_EC_MAX_RETRY'):
        match = re.search(r'^#define\s+'+name+r'\s+(0x[0-9a-fA-F]+|\d+)\s*$', header.decode(), re.M)
        if not match:
            parser.error('missing simple literal macro '+name)
        defines.append(match.group(0))
    receipts = []
    started = datetime.now(timezone.utc).isoformat()
    wall_start = time.monotonic_ns()
    oracle = Oracle()

    def run(command, cwd):
        proc = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                              timeout=30, env={**os.environ, 'TMPDIR': str(cwd), 'LC_ALL': 'C'})
        receipts.append(dict(command=command, rc=proc.returncode, stderr=proc.stderr,
                             stdout=proc.stdout if command[0] in ('gcc', 'git') else None))
        if proc.returncode:
            raise RuntimeError(json.dumps(receipts[-1]))
        return proc.stdout

    results: dict[str, Any] = dict(started_utc=started, source_sha256=digest(raw), header_sha256=digest(header),
                   patch_sha256=PATCH_SHA256, harness_sha256=digest(Path(__file__).read_bytes()),
                   compiler=run(['gcc', '--version'], scratch_root).splitlines()[0],
                   cases_per_variant=len(list(cases())), variants={})
    with tempfile.TemporaryDirectory(prefix='proxnap-driver-latency-', dir=scratch_root) as temp:
        scratch = Path(temp)
        (scratch/'src').mkdir()
        target = scratch/'src/qnap8528.c'
        target.write_bytes(raw)
        run(['git', 'apply', '--check', str(patch)], scratch)
        run(['git', 'apply', str(patch)], scratch)
        source = raw.decode()
        candidate = target.read_text()
        results['candidate_sha256'] = digest(target.read_bytes())
        for name in WAIT_NAMES + ['qnap8528_ec_send_command']:
            oracle.equal(extract(candidate, name), extract(source, name), 'transport literal unchanged '+name)
        for public in ('qnap8528_ec_read', 'qnap8528_ec_write'):
            private = '__'+public+'_locked'
            expected = extract(source, public).replace(public+'(', private+'(', 1).replace(
                'mutex_lock(&qnap8528_ec_lock);', 'lockdep_assert_held(&qnap8528_ec_lock);', 1).replace(
                '\n\tmutex_unlock(&qnap8528_ec_lock);', '', 1)
            oracle.equal(extract(candidate, private), expected, 'private transport body preserved '+public)
        for label, text in (('baseline', source), ('candidate', candidate)):
            names = FUNCTIONS.copy()
            if label == 'candidate':
                names.insert(names.index('qnap8528_ec_read'), '__qnap8528_ec_read_locked')
                names.insert(names.index('qnap8528_ec_write'), '__qnap8528_ec_write_locked')
            fragments = {name: extract(text, name) for name in names}
            c = '\n'.join(defines)+'\n'+PRELUDE+'\n'+'\n\n'.join(fragments.values())+'\n'+MAIN
            cfile = scratch/(label+'.c')
            binary = scratch/label
            cfile.write_text(c)
            # Captured unsigned fan/value comparisons trigger GCC type-limits;
            # retain those diagnostics as warnings, keep all others fatal.
            command = ['gcc', '-std=c11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                       '-Wno-error=type-limits', '-fsanitize=undefined',
                       '-fno-sanitize-recover=all', str(cfile), '-o', str(binary)]
            run(command, scratch)
            rows = []
            before = oracle.assertions
            for case in cases():
                output = run([str(binary), case['op'], str(case['failure']),
                              str(case['prior_bad']), str(case['direct_bad'])], scratch)
                result = json.loads(output)
                oracle.verify(case, result, label == 'candidate')
                rows.append(dict(case=case, result=result))
            results['variants'][label] = dict(cases=len(rows), c_assertions=sum(x['result']['c_assertions'] for x in rows),
                oracle_assertions=oracle.assertions-before, generated_c_sha256=digest(c.encode()),
                extracted_functions_sha256={name: digest(body.encode()) for name, body in fragments.items()},
                binary_sha256=digest(binary.read_bytes()), rows=rows)
        # Compare all observations except intentionally coarser candidate mutex scope.
        before = oracle.assertions
        for base, cand in zip(results['variants']['baseline']['rows'], results['variants']['candidate']['rows'], strict=True):
            for key in ('rc','ebusy','data','ram155','mode','duty','reads','writes','tails','clock_us'):
                oracle.equal(base['result'][key], cand['result'][key], 'baseline/candidate '+key)
        results['cross_variant_assertions'] = oracle.assertions-before
    results.update(scratch_path=temp, scratch_removed=not Path(temp).exists(),
                   finished_utc=datetime.now(timezone.utc).isoformat(),
                   elapsed_wall_ns=time.monotonic_ns()-wall_start,
                   oracle_assertions_total=oracle.assertions,
                   c_assertions_total=sum(v['c_assertions'] for v in results['variants'].values()),
                   cases_total=sum(v['cases'] for v in results['variants'].values()),
                   receipts=receipts,
                   limits=['logical delay counters, not measured physical latency',
                           'RAM status scripts, no EC/port IO and no autonomous hardware validation',
                           'mutex instrumentation, not real kernel lockdep or concurrent scheduling',
                           'host serialization does not latch, ACK or roll back paired EC commands',
                           'fan channel 6 only; channel mapping covered by separate unchanged suite',
                           'all real transport timeout paths return EBUSY; no distinct errno injection'])
    if not results['scratch_removed']:
        raise RuntimeError('owned scratch remains')
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
