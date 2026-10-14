"""Experimental hardware standard-ID filter test; two separate terminated buses."""
import argparse
from collections import Counter
import json
from pathlib import Path
import select
import socket
import struct
import subprocess
import time

from .kernel_test import ip, snapshot, stats, check_state, validate_vector, RATES
from .socketcan_independent import validate_peer

IDS = (0, 0x11f, 0x120, 0x123, 0x124, 0x12f, 0x130, 0x7ff)
PROFILES = ((0, 0), (1, 0), (2, 0), (0, 1), (0, 2), (0, 0))


def accepts(profile, identifier):
    return profile == 0 or (profile == 1 and identifier == 0x123) or (
        profile == 2 and 0x120 <= identifier <= 0x12f)


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=15)


def down(names):
    for name in names:
        ip('link', 'set', 'dev', name, 'down')


def reload_module(profiles, names):
    down(names)
    run('rmmod', 'cancasexl')
    run('modprobe', 'cancasexl', 'std_filter=' + ','.join(map(str, profiles)),
        'diagnose_channel=0', 'isolate_can_errors=1')
    deadline = time.monotonic() + 5
    while not all((Path('/sys/class/net') / n).exists() for n in names):
        if time.monotonic() > deadline:
            raise RuntimeError('Interfaces missing after module reload; inspect dmesg')
        time.sleep(.05)
    actual = Path('/sys/module/cancasexl/parameters/std_filter').read_text().strip()
    if actual != ','.join(map(str, profiles)):
        raise RuntimeError(f'Unexpected filter parameters: {actual}')
    for i, n in enumerate(names):
        validate_vector(n)
        ip('link', 'set', 'dev', n, 'type', 'can', 'bitrate', str(RATES[i]))
        ip('link', 'set', 'dev', n, 'up')


def traffic(names, peers, profiles):
    interfaces = list(names) + list(peers)
    before = {n: snapshot(n) for n in interfaces}
    sockets = {}
    expected = {n: Counter() for n in interfaces}
    received = {n: Counter() for n in interfaces}
    try:
        for n in interfaces:
            s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
            sockets[n] = s
            # No socket-level ID filters: RX statistics must also match the
            # accepted set, distinguishing hardware filtering from user filtering.
            s.bind((n,))
            s.setblocking(False)

        def collect(seconds):
            end = time.monotonic() + seconds
            reverse = {s: n for n, s in sockets.items()}
            while time.monotonic() < end:
                ready, _, _ = select.select(list(reverse), [], [], max(0, end-time.monotonic()))
                for s in ready:
                    data = s.recv(16)
                    if len(data) != 16:
                        raise RuntimeError('Unexpected CAN frame size')
                    ident, dlc, payload = struct.unpack('=IB3x8s', data)
                    received[reverse[s]][(ident, dlc, payload)] += 1

        for repeat in range(5):
            for ident in IDS:
                for i in range(2):
                    payload = struct.pack('=II', repeat, i)
                    key = (ident, 8, payload)
                    sockets[peers[i]].send(struct.pack('=IB3x8s', *key))
                    if accepts(profiles[i], ident):
                        expected[names[i]][key] += 1
                    # Outgoing ID deliberately outside restrictive RX profiles.
                    txkey = (0x6a0+i, 8, struct.pack('=II', repeat, ident))
                    sockets[names[i]].send(struct.pack('=IB3x8s', *txkey))
                    expected[peers[i]][txkey] += 1
                collect(.02)
        collect(.5)
        after = {n: snapshot(n) for n in interfaces}
        errors = []
        for n in interfaces:
            if received[n] != expected[n]:
                errors.append(f'{n}: missing={sum((expected[n]-received[n]).values())}, '
                              f'unexpected={sum((received[n]-expected[n]).values())}')
            for direction in ('rx', 'tx'):
                for k in ('errors', 'dropped'):
                    if stats(after[n])[direction][k] != stats(before[n])[direction][k]:
                        errors.append(f'{n}: {direction}.{k} increased')
        for i, n in enumerate(names):
            check_state(after[n], True, RATES[i])
            for direction, count in (('rx', sum(expected[n].values())), ('tx', 40)):
                a, b = stats(before[n])[direction], stats(after[n])[direction]
                if b['packets']-a['packets'] != count or b['bytes']-a['bytes'] != count*8:
                    errors.append(f'{n}: hardware {direction} counters do not match')
        return dict(profiles=profiles, before=before, after=after,
                    received={n: sum(v.values()) for n, v in received.items()},
                    expected={n: sum(v.values()) for n, v in expected.items()}, errors=errors)
    finally:
        for s in sockets.values():
            s.close()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--interfaces', nargs=2, default=['canxlk0', 'canxlk1'])
    p.add_argument('--peers', nargs=2, default=['can1', 'can0'])
    p.add_argument('--report', required=True)
    args = p.parse_args(argv)
    report = dict(passed=False, phases=[], cleanup_errors=[])
    changed = False
    with open(args.report, 'x', encoding='utf-8') as output:
        try:
            if len(set(args.interfaces + args.peers)) != 4:
                raise RuntimeError('Four distinct interfaces required')
            version = Path('/sys/module/cancasexl/version').read_text().strip()
            report['module_version'] = version
            if version != '0.1.9' or run('modinfo', '-F', 'version', 'cancasexl').strip() != version:
                raise RuntimeError('Loaded AND installed module 0.1.9 required')
            for i, n in enumerate(args.interfaces):
                validate_vector(n)
                if 'UP' in snapshot(n)['flags']:
                    raise RuntimeError(f'{n}: stop channel before test')
                validate_peer([snapshot(args.peers[i])], args.peers[i], RATES[i])
            owned = {p.name for p in Path('/sys/class/net').iterdir()
                     if (p/'device/driver').is_symlink()
                     and (p/'device/driver').resolve().name == 'cancasexl'}
            if owned != set(args.interfaces):
                raise RuntimeError('Test requires exactly one CANcase XL / two owned interfaces')
            changed = True
            for profiles in PROFILES:
                reload_module(profiles, args.interfaces)
                for reopen in (None, 0, 1):
                    if reopen is not None:
                        # Opening one channel must not reset the other's filter.
                        ip('link', 'set', 'dev', args.interfaces[reopen], 'down')
                        ip('link', 'set', 'dev', args.interfaces[reopen], 'up')
                    result = traffic(args.interfaces, args.peers, profiles)
                    result['reopened_channel'] = None if reopen is None else reopen + 1
                    report['phases'].append(result)
                    print(f'filters={profiles}, reopen={reopen}: {result["errors"] or "PASS"}', flush=True)
                    if result['errors']:
                        raise RuntimeError('; '.join(result['errors']))
            report['passed'] = True
        except Exception as exc:
            report['error'] = str(exc)
            print('Filter test:', exc)
        finally:
            if changed:
                try:
                    # Restore open filters; leave Vector channels stopped.
                    if Path('/sys/module/cancasexl').exists():
                        down(args.interfaces)
                        run('rmmod', 'cancasexl')
                    run('modprobe', 'cancasexl', 'std_filter=0,0', 'diagnose_channel=0', 'isolate_can_errors=1')
                except Exception as exc:
                    report['cleanup_errors'].append(str(exc))
                    report['passed'] = False
            json.dump(report, output, indent=2)
            output.write('\n')
    print(('PASS' if report['passed'] else 'FAIL') + ' Report: ' + args.report)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
