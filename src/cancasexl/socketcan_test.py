"""Automated physical loopback test through the running SocketCAN bridge.
Connect only CANcase XL channels 1+2 to one terminated test bus. No other sender.
This test changes both channel bitrates and leaves both stopped afterwards.
"""
import argparse
import json
import math
from pathlib import Path
import secrets
import select
import socket
import struct
import time
from .driver import CANFrame
from .socketcan import pack_frame, unpack_frame, RATES


def control(path, **request):
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as sock:
        sock.settimeout(15)
        sock.connect(path)
        sock.sendall(json.dumps(request).encode())
        result = json.loads(sock.recv(16384))
    if not result.get('ok'):
        raise RuntimeError(result.get('error', 'control failed'))
    return result


def exchange(sockets, pairs=100, timeout=2.0, *, seconds=None, rate=0,
             metrics=None, progress=None):
    token = secrets.randbits(32)
    rx = [0, 0]
    submitted = [0, 0]
    start = time.monotonic()
    end = start + seconds if seconds is not None else None
    next_send = start
    next_progress = start + 10
    completed = 0
    result = metrics if metrics is not None else {}
    result.update(pairs=0, rx=rx, tx_submitted=submitted,
                  requested_seconds=seconds, rate_limit_per_channel=rate)
    try:
        while seconds is not None or completed < pairs:
            now = time.monotonic()
            if end is not None and now >= end:
                break
            if now < next_send:
                delay = next_send-now
                if end is not None:
                    delay = min(delay, end-now)
                time.sleep(delay)
                now = time.monotonic()
                if end is not None and now >= end:
                    break
            # No catch-up bursts when the host or hardware was slower than target.
            next_send = now + 1/rate if rate else now
            frames = [CANFrame(0x601+c, struct.pack('<II', token, completed)) for c in (0, 1)]
            # Submit BOTH before receiving. Bridge services both independently.
            for c in (0, 1):
                sockets[c].sendall(pack_frame(frames[c]))
                submitted[c] += 1
            waiting = {0, 1}
            deadline = time.monotonic() + timeout
            while waiting:
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f'pair {completed}: missing RX on channels {sorted(c+1 for c in waiting)}')
                ready, _, _ = select.select(sockets, [], [], remaining)
                for sock in ready:
                    c = sockets.index(sock)
                    frame = unpack_frame(sock.recv(72))
                    if c not in waiting or frame != frames[1-c]:
                        raise RuntimeError(f'pair {completed}: unexpected/duplicate frame on channel {c+1}: {frame}')
                    waiting.remove(c)
                    rx[c] += 1
            completed += 1
            result['pairs'] = completed
            now = time.monotonic()
            if progress is not None and now >= next_progress:
                progress(completed, now-start)
                next_progress = now + 10
        if not completed:
            raise RuntimeError('test duration elapsed before the first pair completed')
        if select.select(sockets, [], [], 0.05)[0]:
            raise RuntimeError('unexpected trailing SocketCAN frame')
        return result
    finally:
        elapsed = time.monotonic()-start
        result.update(seconds=elapsed,
                      achieved_frames_per_second_per_channel=completed/elapsed if elapsed else 0)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', default='/run/cancasexl/control.sock')
    parser.add_argument('--bitrates', nargs='+', type=int, choices=RATES, default=list(RATES))
    amount = parser.add_mutually_exclusive_group()
    amount.add_argument('--pairs', type=int, help='pairs per bitrate (default: 100)')
    amount.add_argument('--seconds', type=float, help='duration per bitrate instead of a fixed pair count')
    parser.add_argument('--rate', type=float, default=0, help='max frames/s per channel: 1..1000; 0 = unpaced')
    parser.add_argument('--report', default='socketcan_report.json')
    args = parser.parse_args(argv)
    if args.pairs is None:
        args.pairs = 100
    if not 1 <= args.pairs <= 10000:
        parser.error('--pairs must be 1..10000')
    if args.seconds is not None and (not math.isfinite(args.seconds) or not 0 < args.seconds <= 86400):
        parser.error('--seconds must be finite and in (0, 86400]')
    if not math.isfinite(args.rate) or (args.rate != 0 and not 1 <= args.rate <= 1000):
        parser.error('--rate must be 0 or in 1..1000')
    report = dict(started=time.time(), cases=[], passed=False, cleanup_errors=[])
    result = 1
    connected = False
    # Refuse overwrite before touching the running hardware.
    with open(args.report, 'x', encoding='utf-8') as output:
        try:
            initial = control(args.socket, action='status')
            if initial.get('mode') != 'userspace-vxcan-bridge':
                raise RuntimeError('unexpected control service')
            connected = True
            names = [c['interface'] for c in initial['channels']]
            for rate in args.bitrates:
                case = dict(bitrate=rate, passed=False)
                report['cases'].append(case)
                sockets = []
                try:
                    for ch in (1, 2): control(args.socket, action='down', channel=ch)
                    for ch in (1, 2): control(args.socket, action='up', channel=ch, bitrate=rate)
                    before = control(args.socket, action='status')
                    for name in names:
                        sock = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
                        sockets.append(sock)
                        sock.settimeout(2)
                        sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_RECV_OWN_MSGS, 0)
                        sock.bind((name,))
                    print(f'{rate}: starting, duration={args.seconds}, rate limit={args.rate}/s per channel', flush=True)
                    exchange(sockets, args.pairs, seconds=args.seconds, rate=args.rate, metrics=case,
                             progress=lambda count, elapsed: print(
                                 f'{rate}: {elapsed:.0f}s, {count} pairs received', flush=True))
                    expected = case['pairs']
                    # RX arriving at the other channel is not a local TX receipt.
                    deadline = time.monotonic()+2
                    while True:
                        after = control(args.socket, action='status')
                        counts = [after['channels'][c]['counters'].get('tx_confirmed',0) -
                                  before['channels'][c]['counters'].get('tx_confirmed',0) for c in (0,1)]
                        if counts == [expected, expected]:
                            break
                        if any(n > expected for n in counts) or time.monotonic() >= deadline:
                            raise RuntimeError(f'physical TX confirmation mismatch: {counts}')
                        time.sleep(0.02)
                    for c in (0,1):
                        for key in ('tag_2','hardware_overrun_events','tx_orphan_receipts',
                                    'tx_rejected_format','tx_cancelled'):
                            if after['channels'][c]['counters'].get(key,0) != before['channels'][c]['counters'].get(key,0):
                                raise RuntimeError(f'channel {c+1}: {key} increased')
                    case.update(passed=True, tx_confirmed=counts)
                    print(f'{rate}: PASS, {expected} pairs, both directions', flush=True)
                except Exception as exc:
                    case['error'] = str(exc)
                    raise
                finally:
                    for sock in sockets: sock.close()
            report['passed'] = True
            result = 0
        except KeyboardInterrupt:
            report['error'] = 'interrupted'
            result = 130
        except Exception as exc:
            report['error'] = str(exc)
            print('SocketCAN test: ' + str(exc), flush=True)
        finally:
            if connected:
                for ch in (1,2):
                    try: control(args.socket, action='down', channel=ch)
                    except Exception as exc: report['cleanup_errors'].append(str(exc))
            if report['cleanup_errors']:
                report['passed'] = False
                result = 1
            report['finished'] = time.time()
            json.dump(report, output, indent=2)
            output.write('\n')
    print('Report:', str(Path(args.report).resolve()))
    return result


if __name__ == '__main__':
    raise SystemExit(main())
