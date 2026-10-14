"""Kernel CANcase XL test on two separate buses with external SocketCAN peers."""
import argparse
import json
import math
from pathlib import Path
import subprocess
import time
from .socketcan_independent import Traffic, validate_peer, read_bitrate
from .socketcan_dualbus import measure
from .kernel_stream import StreamTraffic
from . import cpu_measure

RATES = (500000, 1000000)


def ip(*args):
    return subprocess.check_output(['ip', *args], text=True, stderr=subprocess.STDOUT, timeout=10)


def snapshot(name):
    rows = json.loads(ip('-j', '-details', '-statistics', 'link', 'show', 'dev', name))
    if len(rows) != 1 or rows[0].get('ifname') != name:
        raise RuntimeError(f'{name}: interface not found')
    return rows[0]


def stats(row):
    value = row.get('stats64', row.get('stats'))
    if not isinstance(value, dict) or not all(k in value for k in ('rx', 'tx')):
        raise RuntimeError(f"{row.get('ifname')}: missing RX/TX statistics")
    for direction in ('rx', 'tx'):
        for key in ('packets', 'bytes', 'errors', 'dropped'):
            if type(value[direction].get(key)) is not int:
                raise RuntimeError(f'missing {direction}.{key} counter')
    return value


def verify_counts(before, after, pairs, *, peer=False, payload_bytes=None):
    payload_bytes = pairs * 8 if payload_bytes is None else payload_bytes
    a, b = stats(before), stats(after)
    for direction in ('rx', 'tx'):
        for key in ('errors', 'dropped', 'over_errors', 'fifo_errors', 'missed_errors',
                    'carrier_errors', 'aborted_errors'):
            if b[direction].get(key, 0) != a[direction].get(key, 0):
                raise RuntimeError(f"{after['ifname']}: {direction}.{key} changed")
        expected_pairs = pairs
        if peer and direction == 'rx':
            # Some peer drivers include local TX echo in RX-offload statistics.
            # Only the two explicit conventions are allowed; socket payloads
            # are still checked independently, exactly once per direction.
            observed = b['rx']['packets'] - a['rx']['packets']
            if observed not in (pairs, 2 * pairs):
                raise RuntimeError(f"{after['ifname']}: peer RX delta={observed}, expected {pairs} or {2*pairs} with echo")
            expected_pairs = observed
        for key, expected in (('packets', expected_pairs), ('bytes', payload_bytes * (2 if pairs and expected_pairs == 2*pairs else 1))):
            delta = b[direction][key] - a[direction][key]
            if delta != expected:
                raise RuntimeError(f"{after['ifname']}: {direction}.{key} delta={delta}, expected={expected}")


def check_state(row, active, rate):
    data = row.get('linkinfo', {}).get('info_data', {})
    if row.get('linkinfo', {}).get('info_kind') != 'can':
        raise RuntimeError('expected physical CAN interface')
    if ('UP' in row.get('flags', [])) != active or data.get('state') != ('ERROR-ACTIVE' if active else 'STOPPED'):
        raise RuntimeError(f"{row['ifname']}: unexpected channel state")
    if read_bitrate(data) != rate:
        raise RuntimeError(f"{row['ifname']}: unexpected bitrate")
    if active and 'LOWER_UP' not in row.get('flags', []):
        raise RuntimeError(f"{row['ifname']}: carrier lost")


def validate_vector(name):
    driver = Path('/sys/class/net') / name / 'device/driver'
    if not driver.is_symlink() or driver.resolve().name != 'cancasexl':
        raise RuntimeError(f'{name}: not owned by the cancasexl kernel driver')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interfaces', nargs=2, default=['canxlk0', 'canxlk1'])
    parser.add_argument('--peers', nargs=2, default=['can0', 'can1'])
    parser.add_argument('--seconds', type=float, default=10, help='initial simultaneous traffic duration')
    parser.add_argument('--rate', type=float, default=100, help='target pairs/s/channel (actual rate reported)')
    parser.add_argument('--profile', choices=('standard', 'mixed'), default='standard', help='mixed: all DLCs 0..8 with standard and extended IDs')
    parser.add_argument('--traffic', choices=('paired', 'stream'), default='paired')
    parser.add_argument('--window', type=int, default=4, help='stream: maximum unreceived frames per direction (1..8)')
    parser.add_argument('--report', default='kernel_dualbus_report.json')
    args = parser.parse_args(argv)
    if not math.isfinite(args.seconds) or args.seconds < 1:
        parser.error('--seconds must be finite and >= 1')
    if not math.isfinite(args.rate) or not 1 <= args.rate <= 10000:
        parser.error('--rate must be finite and between 1 and 10000')
    if not 1 <= args.window <= 8:
        parser.error('--window must be between 1 and 8')
    if args.traffic == 'stream' and args.profile != 'standard':
        parser.error('stream requires --profile standard (8-byte sequence frames)')
    names = args.interfaces + args.peers
    if len(set(names)) != 4 or any('/' in n or not n or n.startswith('-') for n in names):
        parser.error('four distinct interface names required')
    report = dict(started=time.time(), mode='kernel-dualbus', interfaces=args.interfaces,
                  peers=args.peers, bitrates=RATES, rate_limit=args.rate, profile=args.profile, traffic_mode=args.traffic, window=args.window, phases=[], segments=[], confirmations=[],
                  passed=False, cleanup_errors=[])
    try:
        output = open(args.report, 'x', encoding='utf-8')
    except OSError as exc:
        print(f'Report cannot be created: {exc}. Choose a fresh filename.', flush=True)
        return 1
    workers = [None, None]
    totals = [0, 0]
    byte_totals = [0, 0]
    touched = False
    baseline = None
    try:
        # Complete preflight before any channel mutation.
        before = {n: snapshot(n) for n in names}
        for i in (0, 1):
            validate_vector(args.interfaces[i])
            validate_peer([before[args.peers[i]]], args.peers[i], RATES[i])
        for row in before.values(): stats(row)
        report['before'] = before
        report['module'] = {}
        for field, path in [('version', '/sys/module/cancasexl/version'), ('fast_poll', '/sys/module/cancasexl/parameters/fast_poll')]:
            try: report['module'][field] = Path(path).read_text().strip()
            except OSError: report['module'][field] = None
        touched = True
        for i, name in enumerate(args.interfaces):
            ip('link', 'set', 'dev', name, 'down')
            ip('link', 'set', 'dev', name, 'type', 'can', 'bitrate', str(RATES[i]))
            ip('link', 'set', 'dev', name, 'up')
        baseline = {n: snapshot(n) for n in names}
        report['baseline'] = baseline
        for i, name in enumerate(args.interfaces): check_state(baseline[name], True, RATES[i])

        def start(i):
            factory = StreamTraffic if args.traffic == 'stream' else Traffic
            options = dict(window=args.window) if args.traffic == 'stream' else {}
            workers[i] = factory(args.interfaces[i], args.peers[i], rate=args.rate, profile=args.profile, **options)
            workers[i].thread.start()

        def finish(i):
            w = workers[i]
            if w is None: return
            try:
                w.finish()
            finally:
                metrics = w.snapshot()
                report['segments'].append(dict(channel=i+1, traffic=metrics, error=w.error))
                totals[i] += metrics['pairs']
                byte_totals[i] += metrics['payload_bytes']
                workers[i] = None

        def confirm(i):
            # Peer RX may precede delivery of the Vector hardware echo.
            deadline = time.monotonic() + 2
            while True:
                current = {n: snapshot(n) for n in (args.interfaces[i], args.peers[i])}
                vector = args.interfaces[i]
                delta = stats(current[vector])['tx']['packets'] - stats(baseline[vector])['tx']['packets']
                if delta >= totals[i] or time.monotonic() >= deadline: break
                time.sleep(0.02)
            report['confirmations'].append(dict(channel=i+1, pairs=totals[i], status=current))
            for n in current:
                verify_counts(baseline[n], current[n], totals[i], peer=n == args.peers[i], payload_bytes=byte_totals[i])

        def phase(name, seconds, active):
            print(name, flush=True)
            cpu_before = cpu_measure.snapshot()
            traffic = measure(workers, seconds)
            cpu = cpu_measure.delta(cpu_before, cpu_measure.snapshot())
            rows = {n: snapshot(n) for n in names}
            for i, n in enumerate(args.interfaces): check_state(rows[n], active[i], RATES[i])
            for i, n in enumerate(args.peers): validate_peer([rows[n]], n, RATES[i])
            report['phases'].append(dict(name=name, traffic=traffic, status=rows, cpu=cpu))

        for i in (0, 1): start(i)
        phase('both_buses_active', args.seconds, [True, True])
        for target in (0, 1):
            finish(target)
            confirm(target)
            name = args.interfaces[target]
            ip('link', 'set', 'dev', name, 'down')
            stopped = snapshot(name)
            active = [True, True]; active[target] = False
            phase(f'channel_{target+1}_down', 3, active)
            verify_counts(stopped, snapshot(name), 0)
            ip('link', 'set', 'dev', name, 'up')
            start(target)
            phase(f'channel_{target+1}_restarted', 3, [True, True])
        for i in (0, 1): finish(i)
        for i in (0, 1): confirm(i)
        if args.profile == 'mixed':
            expected = {f'{fmt}_dlc{dlc}' for fmt in ('standard', 'extended') for dlc in range(9)}
            for i in (0, 1):
                covered = set().union(*(set(s['traffic']['variants']) for s in report['segments'] if s['channel'] == i+1))
                if covered != expected:
                    raise RuntimeError(f'channel {i+1}: incomplete frame variant coverage; increase --seconds/--rate')
        report['final_status'] = {n: snapshot(n) for n in names}
        report['passed'] = True
    except (Exception, KeyboardInterrupt) as exc:
        report['error'] = (exc.output if isinstance(exc, subprocess.CalledProcessError) else str(exc)) or 'interrupted'
        print('Kernel test: ' + report['error'], flush=True)
    finally:
        for w in workers:
            if w: w.stop.set()
        for i, w in enumerate(workers):
            if not w: continue
            try: w.finish()
            except Exception as exc: report['cleanup_errors'].append(str(exc))
            metrics = w.snapshot()
            totals[i] += metrics['pairs']
            byte_totals[i] += metrics['payload_bytes']
            report['segments'].append(dict(channel=i+1, traffic=metrics, error=w.error))
        if touched:
            for name in args.interfaces:
                try: ip('link', 'set', 'dev', name, 'down')
                except Exception as exc: report['cleanup_errors'].append(f'{name}: {exc}')
            try:
                report['after_cleanup'] = {n: snapshot(n) for n in args.interfaces}
                for i, n in enumerate(args.interfaces): check_state(report['after_cleanup'][n], False, RATES[i])
            except Exception as exc: report['cleanup_errors'].append(str(exc))
        if report['cleanup_errors']: report['passed'] = False
        report['pairs_per_channel'] = totals
        report['payload_bytes_per_direction_per_channel'] = byte_totals
        report['finished'] = time.time()
        with output:
            json.dump(report, output, indent=2)
            output.write('\n')
    print('PASS' if report['passed'] else 'FAIL', 'Report:', Path(args.report).resolve(), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
