"""Automatic ACK isolation/recovery test on two separate, terminated CAN buses."""
import argparse
import json
import math
from pathlib import Path
import socket
import struct
import time
from .kernel_test import (ip, snapshot, stats, check_state, validate_vector,
                          validate_peer, verify_counts, RATES)
from .kernel_stream import StreamTraffic


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def peer_mode(name, listen):
    # Preserve bitrate and all other peer settings. Caller preflights normal mode.
    ip('link', 'set', 'dev', name, 'down')
    ip('link', 'set', 'dev', name, 'type', 'can', 'listen-only', 'on' if listen else 'off')
    ip('link', 'set', 'dev', name, 'up')


def isolated(row):
    return (row['linkinfo']['info_data'].get('state') == 'STOPPED'
            and 'UP' in row.get('flags', []) and 'LOWER_UP' not in row.get('flags', []))


def error_socket(name):
    sock = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    try:
        sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER, b'')
        sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_ERR_FILTER, struct.pack('=I', 0x1fffffff))
        sock.settimeout(0.1)
        sock.bind((name,))
        return sock
    except BaseException:
        sock.close()
        raise


def decode_error(wire):
    require(len(wire) == 16, 'unexpected error datagram length')
    identifier, dlc, data = struct.unpack('=IB3x8s', wire)
    require(identifier & 0x20000000 and dlc == 8, 'not a classical CAN error frame')
    return dict(can_id=f'{identifier:08x}', data=data.hex(), ack=bool(identifier & 0x20),
                bus_off=bool(identifier & 0x40))


def check_error_delta(before, after):
    a, b = stats(before), stats(after)
    require(b['tx']['packets'] == a['tx']['packets'], 'unacknowledged frame counted as TX success')
    require(b['tx']['errors'] > a['tx']['errors'], 'missing TX error counter')
    require(after['linkinfo']['info_xstats']['bus_error'] > before['linkinfo']['info_xstats']['bus_error'],
            'missing CAN bus error counter')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interfaces', nargs=2, default=['canxlk0', 'canxlk1'])
    parser.add_argument('--peers', nargs=2, required=True, help='peers for channel 1 (500k), channel 2 (1M)')
    parser.add_argument('--seconds', type=float, default=5, help='duration of each observation phase')
    parser.add_argument('--rate', type=float, default=100, help='frames/s per direction on healthy bus')
    parser.add_argument('--report', default='kernel_ack_report.json')
    args = parser.parse_args(argv)
    names = args.interfaces + args.peers
    if len(set(names)) != 4 or any(not n or '/' in n or n.startswith('-') for n in names):
        parser.error('four distinct interface names required')
    if not math.isfinite(args.seconds) or not 2 <= args.seconds <= 60:
        parser.error('--seconds must be between 2 and 60')
    if not math.isfinite(args.rate) or not 10 <= args.rate <= 1000:
        parser.error('--rate must be between 10 and 1000')
    report = dict(mode='kernel-ack-isolation', started=time.time(), interfaces=args.interfaces,
                  peers=args.peers, bitrates=RATES, passed=False, cases=[], cleanup_errors=[],
                  hardware_busoff_verified=False)
    try:
        output = open(args.report, 'x', encoding='utf-8')
    except OSError as exc:
        print(f'Report cannot be created: {exc}; choose a fresh filename.')
        return 1
    workers = {}
    touched = False
    changed_peers = set()

    def start(i):
        w = StreamTraffic(args.interfaces[i], args.peers[i], rate=args.rate, window=4)
        workers[i] = w
        w.thread.start()

    def observe(label, indices, seconds):
        print(label, flush=True)
        initial = {i: workers[i].snapshot()['pairs'] for i in indices}
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            for i in indices:
                require(workers[i].error is None, f'channel {i+1}: {workers[i].error}')
                require(workers[i].thread.is_alive(), f'channel {i+1}: traffic worker stopped')
            time.sleep(0.05)
        result = {}
        for i in indices:
            m = workers[i].snapshot()
            require(m['pairs'] > initial[i], f'channel {i+1}: no traffic progress')
            check_state(snapshot(args.interfaces[i]), True, RATES[i])
            result[str(i+1)] = dict(pairs_in_phase=m['pairs']-initial[i], traffic=m)
        return dict(name=label, traffic=result)

    def finish(i, baseline, case):
        w = workers[i]
        try:
            w.finish()
        finally:
            case.setdefault('segments', []).append(dict(channel=i+1, traffic=w.snapshot(), error=w.error))
            if not w.thread.is_alive():
                workers.pop(i, None)
        m = w.snapshot()
        require(m['submitted'] == m['received'], 'unreceived traffic after drain')
        end = time.monotonic()+2
        while stats(snapshot(args.interfaces[i]))['tx']['packets']-stats(baseline[args.interfaces[i]])['tx']['packets'] < m['pairs'] and time.monotonic() < end:
            time.sleep(0.02)
        for n in (args.interfaces[i], args.peers[i]):
            row = snapshot(n)
            verify_counts(baseline[n], row, m['pairs'], peer=n == args.peers[i])
            require(row['linkinfo'].get('info_xstats') == baseline[n]['linkinfo'].get('info_xstats'),
                    f'{n}: new CAN error/state counters during normal traffic')

    try:
        # No link mutation before all validation has succeeded.
        report['before'] = {n: snapshot(n) for n in names}
        require(Path('/sys/module/cancasexl/parameters/isolate_can_errors').read_text().strip() == 'Y',
                'isolate_can_errors must be Y')
        report['module_version'] = Path('/sys/module/cancasexl/version').read_text().strip()
        for i in (0, 1):
            validate_vector(args.interfaces[i])
            peer = report['before'][args.peers[i]]
            validate_peer([peer], args.peers[i], RATES[i])
            data = peer['linkinfo']['info_data']
            require('LISTEN-ONLY' in data.get('ctrlmode_supported', []), 'peer does not advertise listen-only')
            require(data.get('restart_ms') == 0, 'peer restart-ms must be 0')
        for row in report['before'].values():
            stats(row)
        touched = True
        for i, n in enumerate(args.interfaces):
            ip('link', 'set', 'dev', n, 'down')
            ip('link', 'set', 'dev', n, 'type', 'can', 'bitrate', str(RATES[i]))
            ip('link', 'set', 'dev', n, 'up')
        for target in (0, 1):
            healthy = 1-target
            vector, peer = args.interfaces[target], args.peers[target]
            case = dict(channel=target+1, passed=False, phases=[])
            report['cases'].append(case)
            baseline = {n: snapshot(n) for n in names}
            start(healthy)
            case['phases'].append(observe('healthy_bus_baseline', [healthy], args.seconds))
            # Subscribe before provoking the single failing TX.
            with error_socket(vector) as errors:
                changed_peers.add(peer)  # Also restore if mode switching fails midway.
                peer_mode(peer, True)
                mode = snapshot(peer)['linkinfo']['info_data']
                require('LISTEN-ONLY' in mode.get('ctrlmode', []), 'listen-only was not enabled')
                before_error = snapshot(vector)
                with socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW) as tx:
                    tx.settimeout(1)
                    tx.bind((vector,))
                    wire = struct.pack('=IB3x8s', 0x601+target*0x20, 8, bytes.fromhex('1122334455667788'))
                    require(tx.send(wire) == 16, 'short CAN send')
                case['error_frames'] = []
                end = time.monotonic()+3
                while time.monotonic() < end:
                    try:
                        event = decode_error(errors.recv(72))
                        case['error_frames'].append(event)
                    except socket.timeout:
                        pass
                    row = snapshot(vector)
                    if isolated(row) and any(e['ack'] for e in case['error_frames']):
                        break
                case['isolated_status'] = row
                require(isolated(row), 'faulted channel did not enter isolated STOPPED state')
                require(any(e['ack'] for e in case['error_frames']), 'no SocketCAN ACK error received')
                require(not any(e['bus_off'] for e in case['error_frames']), 'unexpected bus-off indication')
                check_error_delta(before_error, row)
            case['phases'].append(observe('other_bus_during_ack_error', [healthy], args.seconds))
            require(isolated(snapshot(vector)), 'faulted channel restarted unexpectedly')
            peer_mode(peer, False)
            validate_peer([snapshot(peer)], peer, RATES[target])
            changed_peers.remove(peer)
            ip('link', 'set', 'dev', vector, 'down')
            ip('link', 'set', 'dev', vector, 'up')
            check_state(snapshot(vector), True, RATES[target])
            recovery = {n: snapshot(n) for n in (vector, peer)}
            start(target)
            case['phases'].append(observe('both_buses_after_recovery', [0, 1], args.seconds))
            finish(target, recovery, case)
            finish(healthy, baseline, case)
            case['passed'] = True
        report['passed'] = True
    except (Exception, KeyboardInterrupt) as exc:
        report['error'] = getattr(exc, 'output', None) or str(exc) or 'interrupted'
        print(f"ACK test: {report['error']}", flush=True)
    finally:
        for w in workers.values():
            w.stop.set()
        for i, w in workers.items():
            try:
                w.finish()
            except Exception as exc:
                report['cleanup_errors'].append(f'worker {i+1}: {exc}')
        # Stop Vector TX first; then restore every peer we attempted to change.
        if touched:
            for n in args.interfaces:
                try:
                    ip('link', 'set', 'dev', n, 'down')
                except Exception as exc:
                    report['cleanup_errors'].append(f'{n}: {exc}')
        for n in sorted(changed_peers):
            try:
                peer_mode(n, False)
                validate_peer([snapshot(n)], n, RATES[args.peers.index(n)])
            except Exception as exc:
                report['cleanup_errors'].append(f'restore {n}: {exc}')
        if touched:
            try:
                report['after_cleanup'] = {n: snapshot(n) for n in names}
                for i, n in enumerate(args.interfaces):
                    check_state(report['after_cleanup'][n], False, RATES[i])
                for i, n in enumerate(args.peers):
                    validate_peer([report['after_cleanup'][n]], n, RATES[i])
            except Exception as exc:
                report['cleanup_errors'].append(str(exc))
        if report['cleanup_errors']:
            report['passed'] = False
        report['finished'] = time.time()
        with output:
            json.dump(report, output, indent=2)
            output.write('\n')
    print('PASS' if report['passed'] else 'FAIL', 'Report:', Path(args.report).resolve())
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
