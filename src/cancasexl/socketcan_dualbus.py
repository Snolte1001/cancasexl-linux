"""Two separate buses: Vector 1 / can0 at 500k, Vector 2 / can1 at 1M.
Concurrent verified traffic; stop/restart each Vector channel independently.
External peers must already be configured; their configuration is not changed.
"""
import argparse
import json
from pathlib import Path
import subprocess
import time
from .socketcan_test import control
from .socketcan_independent import Traffic, validate_peer, check_counters

RATES = (500000, 1000000)


def check_dual_state(status, active):
    if len(status['channels']) != 2:
        raise RuntimeError('expected two Vector channels')
    for i, c in enumerate(status['channels']):
        if c['active'] != active[i] or c['bitrate'] != RATES[i] or (active[i] and c.get('stopped_reason')):
            raise RuntimeError(f'channel {i+1}: unexpected state {c}')


def measure(workers, seconds):
    before = [w.snapshot()['pairs'] if w else None for w in workers]
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        for w in workers:
            if w and w.error: raise RuntimeError(w.error)
        time.sleep(0.02)
    result = []
    for i, w in enumerate(workers):
        if w is None:
            result.append(None)
            continue
        metrics = w.snapshot()
        metrics['pairs_in_phase'] = metrics['pairs']-before[i]
        if metrics['pairs_in_phase'] < 1:
            raise RuntimeError(f'channel {i+1}: no traffic progress')
        result.append(metrics)
    return result


def wait_confirmed(path, baseline, channel, expected):
    deadline = time.monotonic()+2
    while True:
        status = control(path, action='status')
        i = channel-1
        n = status['channels'][i]['counters'].get('tx_confirmed', 0)-baseline['channels'][i]['counters'].get('tx_confirmed', 0)
        if n >= expected or time.monotonic() >= deadline:
            check_counters(baseline, status, channel, expected)
            return status
        time.sleep(0.02)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--peers', nargs=2, default=['can0', 'can1'])
    parser.add_argument('--socket', default='/run/cancasexl/control.sock')
    parser.add_argument('--report', default='socketcan_dualbus_report.json')
    args = parser.parse_args(argv)
    if args.peers[0] == args.peers[1]: parser.error('two distinct external peers required')
    report = dict(started=time.time(), peers=args.peers, bitrates=RATES,
                  phases=[], segments=[], passed=False, cleanup_errors=[])
    workers = [None, None]
    connected = False
    result = 1
    with open(args.report, 'x', encoding='utf-8') as output:
        try:
            initial = control(args.socket, action='status')
            if initial.get('mode') != 'userspace-vxcan-bridge':
                raise RuntimeError('unexpected control service')
            names = [c['interface'] for c in initial['channels']]
            report['peer_before'] = []
            for name, rate in zip(args.peers, RATES):
                if name in names: raise ValueError('peer must be external')
                info = json.loads(subprocess.check_output(
                    ['ip', '-j', '-details', 'link', 'show', 'dev', name], text=True, timeout=5))
                validate_peer(info, name, rate)
                report['peer_before'].append(info)
            connected = True
            for ch in (1, 2): control(args.socket, action='down', channel=ch)
            for i in (0, 1): control(args.socket, action='up', channel=i+1, bitrate=RATES[i])
            baseline = control(args.socket, action='status')
            check_dual_state(baseline, [True, True])
            report['initial_status'] = baseline
            totals = [0, 0]

            def start(i):
                workers[i] = Traffic(names[i], args.peers[i])
                workers[i].thread.start()

            def finish(i):
                worker = workers[i]
                if worker is None: return
                try:
                    worker.finish()
                finally:
                    metrics = worker.snapshot()
                    report['segments'].append(dict(channel=i+1, traffic=metrics, error=worker.error))
                    totals[i] += metrics['pairs']
                    workers[i] = None

            def record(name, seconds, active):
                print(name, flush=True)
                metrics = measure(workers, seconds)
                status = control(args.socket, action='status')
                check_dual_state(status, active)
                report['phases'].append(dict(name=name, traffic=metrics, status=status))

            for i in (0, 1): start(i)
            record('both_buses_active', 3, [True, True])
            for target in (0, 1):
                # Stop only the target generator, complete its pending pair and
                # physical receipt before stopping hardware. Survivor never stops.
                finish(target)
                wait_confirmed(args.socket, baseline, target+1, totals[target])
                control(args.socket, action='down', channel=target+1)
                active = [True, True]; active[target] = False
                record(f'channel_{target+1}_down', 2, active)
                control(args.socket, action='up', channel=target+1, bitrate=RATES[target])
                start(target)
                record(f'channel_{target+1}_restarted', 3, [True, True])
            for i in (0, 1): finish(i)
            for i in (0, 1): wait_confirmed(args.socket, baseline, i+1, totals[i])
            report['final_status'] = control(args.socket, action='status')
            check_dual_state(report['final_status'], [True, True])
            report['pairs_per_channel'] = totals
            report['passed'] = True
            result = 0
        except (Exception, KeyboardInterrupt) as exc:
            report['error'] = str(exc) or 'interrupted'
            print('Dual-bus test: '+report['error'], flush=True)
        finally:
            # Request both workers to finish before waiting for either one.
            for w in workers:
                if w: w.stop.set()
            for i, w in enumerate(workers):
                if w:
                    try: w.finish()
                    except Exception as exc: report['cleanup_errors'].append(str(exc))
                    report['segments'].append(dict(channel=i+1, traffic=w.snapshot(), error=w.error))
            if connected:
                for ch in (1, 2):
                    try: control(args.socket, action='down', channel=ch)
                    except Exception as exc: report['cleanup_errors'].append(str(exc))
            if report['cleanup_errors']:
                report['passed'] = False
                result = 1
            report['finished'] = time.time()
            json.dump(report, output, indent=2)
            output.write('\n')
    print('PASS' if report['passed'] else 'FAIL', 'Report:', Path(args.report).resolve(), flush=True)
    return result


if __name__ == '__main__':
    raise SystemExit(main())
