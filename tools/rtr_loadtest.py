#!/usr/bin/env python3
"""Bidirectional Classical CAN data/RTR test. Existing configuration is unchanged."""
import argparse
import collections
import errno
import json
import select
import socket
import struct
import subprocess
import time
from pathlib import Path

EFF, RTR = 0x80000000, 0x40000000
FRAME = struct.Struct('=IB3x8s')

def frame(n):
    kind = n % 4
    dlc = (n // 4) % 9
    ident = (0x100000 + n) | EFF if kind >= 2 else 0x100 + n % 512
    if kind % 2:
        ident |= RTR
    data = b'' if ident & RTR else n.to_bytes(8, 'little')[:dlc]
    return FRAME.pack(ident, dlc, data.ljust(8, b'\0')), (ident, dlc, data)

def decode(raw):
    if len(raw) != 16:
        raise RuntimeError(f'Unexpected frame size: {len(raw)}')
    ident, dlc, data = FRAME.unpack(raw)
    if dlc > 8:
        raise RuntimeError(f'Unexpected DLC: {dlc}')
    return ident, dlc, b'' if ident & RTR else data[:dlc]

def status(name):
    return json.loads(subprocess.check_output(
        ['ip', '-j', '-details', '-statistics', 'link', 'show', name], text=True))[0]

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--interfaces', nargs=2, required=True)
    p.add_argument('--rate', type=int, default=1000, help='Target frames/s per channel')
    p.add_argument('--seconds', type=float, default=30)
    p.add_argument('--window', type=int, default=4)
    p.add_argument('--report', required=True)
    a = p.parse_args()
    if len(set(a.interfaces)) != 2 or not 1 <= a.rate <= 10000 or not 1 <= a.window <= 8 or not 1 <= a.seconds <= 600:
        p.error('Use distinct interfaces, rate 1..10000, window 1..8, seconds 1..600')
    # Reserve report before any traffic; never overwrite a prior report.
    with Path(a.report).open('x', encoding='utf-8') as output:
        report = {'ok': False, 'settings': vars(a), 'errors': []}
        sockets = []
        sent, received, retries, local = [0, 0], [0, 0], [0, 0], [0, 0]
        pending = [collections.OrderedDict(), collections.OrderedDict()]
        counts = [collections.Counter(), collections.Counter()]
        started = None
        try:
            before = [status(x) for x in a.interfaces]
            report['before'] = before
            for s in before:
                d = s['linkinfo']['info_data']
                rate = d.get('bittiming_bitrate', d.get('bittiming', {}).get('bitrate'))
                if 'UP' not in s['flags'] or d['state'] != 'ERROR-ACTIVE' or rate != 1000000:
                    raise RuntimeError(f"{s['ifname']}: requires UP, ERROR-ACTIVE, 1000000 bit/s")
            for name in a.interfaces:
                s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
                sockets.append(s)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1048576)
                s.bind((name,))
                s.setblocking(False)
            started = time.monotonic()
            end = started + a.seconds
            next_send = [started, started]
            print('Mixed data/RTR test running on ' + ' <-> '.join(a.interfaces), flush=True)
            while True:
                now = time.monotonic()
                for i, s in enumerate(sockets):
                    # A bounded outstanding window avoids flooding the driver queue.
                    if now < end and now >= next_send[i] and len(pending[i]) < a.window:
                        raw, key = frame(sent[i])
                        try:
                            if s.send(raw) != 16:
                                raise RuntimeError('Short socket write')
                        except OSError as e:
                            if e.errno not in (errno.ENOBUFS, errno.EAGAIN):
                                raise
                            retries[i] += 1
                        else:
                            if key in pending[i]:
                                raise RuntimeError('Ambiguous outstanding frame')
                            pending[i][key] = now
                            counts[i][('extended' if key[0] & EFF else 'standard') + ('_rtr' if key[0] & RTR else '_data')] += 1
                            sent[i] += 1
                        next_send[i] = now + 1 / a.rate
                ready, _, _ = select.select(sockets, [], [], 0.0005)
                for s in ready:
                    receiver = sockets.index(s)
                    sender = 1 - receiver
                    for _ in range(256):
                        try:
                            raw, _, flags, _ = s.recvmsg(72)
                        except BlockingIOError:
                            break
                        if flags & socket.MSG_DONTROUTE:
                            local[receiver] += 1
                            raise RuntimeError('Other local sender detected; stop other traffic generators')
                        key = decode(raw)
                        if key not in pending[sender]:
                            raise RuntimeError(f'{a.interfaces[receiver]}: unexpected/duplicate frame {key!r}')
                        del pending[sender][key]
                        received[receiver] += 1
                now = time.monotonic()
                for i in range(2):
                    if pending[i] and now - next(iter(pending[i].values())) > 2:
                        raise RuntimeError(f'{a.interfaces[i]}: frame unreceived for 2 seconds')
                if now >= end and not any(pending):
                    break
            # Check late duplicates/unexpected traffic after all expected receives.
            if select.select(sockets, [], [], 0.2)[0]:
                raise RuntimeError('Unexpected traffic after completion')
            if not all(sent) or received != sent[::-1]:
                raise RuntimeError('Frame counts do not match')
            report['ok'] = True
        except (Exception, KeyboardInterrupt) as e:
            report['errors'].append(str(e) or type(e).__name__)
        finally:
            report.update(sent=sent, received=received, send_backpressure=retries,
                          unexpected_local_frames=local, frame_types=[dict(x) for x in counts],
                          outstanding=[len(x) for x in pending])
            if started is not None:
                report['elapsed_seconds'] = time.monotonic() - started
                report['achieved_frames_per_second_per_channel'] = [round(n / a.seconds, 2) for n in sent]
            for s in sockets:
                s.close()
            try:
                after = [status(x) for x in a.interfaces]
                report['after'] = after
                if 'before' in report:
                    for b, c in zip(report['before'], after):
                        if b['ifindex'] != c['ifindex'] or c['linkinfo']['info_data']['state'] != 'ERROR-ACTIVE':
                            raise RuntimeError('Interface changed or no longer ERROR-ACTIVE')
                        for direction in ('rx', 'tx'):
                            for field in ('errors', 'dropped', 'over_errors', 'carrier_errors'):
                                if c['stats64'][direction].get(field, 0) > b['stats64'][direction].get(field, 0):
                                    raise RuntimeError(f"{c['ifname']}: increased {direction} {field}")
                        for field, value in c['linkinfo'].get('info_xstats', {}).items():
                            if field != 'arbitration_lost' and value > b['linkinfo'].get('info_xstats', {}).get(field, 0):
                                raise RuntimeError(f"{c['ifname']}: increased {field}")
            except Exception as e:
                report['ok'] = False
                report['errors'].append(str(e))
            json.dump(report, output, indent=2)
            output.write('\n')
        print(('PASS' if report['ok'] else 'FAIL') + ': ' + a.report)
        print('TX:', sent, 'RX:', received)
        for error in report['errors']:
            print(error)
        return 0 if report['ok'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
