"""Three-node test: CANcase channels + external SocketCAN peer on one 500k bus.
Stops/restarts each Vector channel while the other exchanges checked frames.
Does not change the peer configuration or switch bitrate on the shared bus.
"""
import argparse
import json
import math
from pathlib import Path
import secrets
import select
import socket
import struct
import subprocess
import threading
import time
from .driver import CANFrame
from .socketcan import pack_frame, unpack_frame
from .socketcan_test import control


def read_bitrate(data):
    """iproute2 JSON: calculated timing or fixed bitrate-list devices."""
    timing = data.get('bittiming', {})
    nested = timing.get('bitrate') if isinstance(timing, dict) else None
    flat = data.get('bittiming_bitrate')
    if nested is not None and flat is not None and nested != flat:
        raise ValueError('conflicting CAN bitrate fields')
    return nested if nested is not None else flat


def validate_peer(info, name, bitrate=500000):
    if len(info) != 1 or info[0].get('ifname') != name:
        raise RuntimeError('external peer not found')
    link = info[0]
    details = link.get('linkinfo', {})
    data = details.get('info_data', {})
    if details.get('info_kind') != 'can' or 'UP' not in link.get('flags', []):
        raise RuntimeError('external peer must be an UP physical CAN interface')
    if read_bitrate(data) != bitrate:
        raise RuntimeError(f'external peer must use {bitrate} bit/s')
    if data.get('state') != 'ERROR-ACTIVE':
        raise RuntimeError('external peer must be ERROR-ACTIVE')
    if any(str(m).upper() in ('LISTEN-ONLY', 'LOOPBACK') for m in data.get('ctrlmode', [])):
        raise RuntimeError('external peer must use normal mode')


def open_can(name, identifier, mixed=False):
    sock = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    try:
        sock.settimeout(1)
        # Exact standard data ID. CAN_ERR_FLAG must NOT be in this mask:
        # it selects the kernel error receive list. Error frames are disabled
        # separately by the default CAN_RAW_ERR_FILTER=0.
        sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER,
                        struct.pack('=II', identifier, 0xC00007FF) +
                        (struct.pack('=II', 0x80000000 | 0x1ABC0000 | identifier, 0xDFFFFFFF) if mixed else b''))
        sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_RECV_OWN_MSGS, 0)
        sock.bind((name,))
        return sock
    except BaseException:
        sock.close()
        raise


def traffic_frames(token, seq, profile='standard'):
    extended = profile == 'mixed' and (seq // 9) % 2 == 1
    length = seq % 9 if profile == 'mixed' else 8
    # Sequence first: even short payloads change between rounds.
    payload = struct.pack('<II', seq, token)[:length]
    return [CANFrame((0x1ABC0000 if extended else 0) | (0x651+i),
                     payload, extended=extended) for i in (0, 1)]


class Traffic:
    def __init__(self, vector, peer, *, rate=100, profile='standard'):
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError('rate must be finite and positive')
        if profile not in ('standard', 'mixed'):
            raise ValueError('unknown traffic profile')
        self.rate = rate
        self.profile = profile
        self.names = (vector, peer)
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.metrics = dict(pairs=0, submitted=[0, 0], received=[0, 0],
                            max_pair_latency_ms=0, max_completion_gap_ms=0,
                            payload_bytes=0, variants={}, elapsed_seconds=0, achieved_pairs_per_second=0)
        self.error = None
        self.thread = threading.Thread(target=self.run, daemon=True)

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.metrics))

    def run(self):
        sockets = []
        try:
            sockets = [open_can(self.names[0], 0x652, self.profile == 'mixed')]
            sockets.append(open_can(self.names[1], 0x651, self.profile == 'mixed'))
            began = time.monotonic()
            token = secrets.randbits(32)
            seq = 0
            last = None
            while not self.stop.is_set():
                start = time.monotonic()
                frames = traffic_frames(token, seq, self.profile)
                for i, sock in enumerate(sockets):
                    sock.sendall(pack_frame(frames[i]))
                    with self.lock: self.metrics['submitted'][i] += 1
                waiting = {0, 1}
                deadline = start+1
                while waiting:
                    remaining = deadline-time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f'pair {seq}: missing RX {sorted(waiting)} (0=Vector, 1=peer)')
                    ready = select.select(sockets, [], [], remaining)[0]
                    for sock in ready:
                        i = sockets.index(sock)
                        frame = unpack_frame(sock.recv(72))
                        if i not in waiting or frame != frames[1-i]:
                            raise RuntimeError(f'pair {seq}: unexpected/duplicate frame on {self.names[i]}')
                        waiting.remove(i)
                        with self.lock: self.metrics['received'][i] += 1
                now = time.monotonic()
                with self.lock:
                    self.metrics['pairs'] += 1
                    self.metrics['payload_bytes'] += len(frames[0].data)
                    variant = f"{'extended' if frames[0].extended else 'standard'}_dlc{len(frames[0].data)}"
                    self.metrics['variants'][variant] = self.metrics['variants'].get(variant, 0)+1
                    self.metrics['elapsed_seconds'] = now-began
                    self.metrics['achieved_pairs_per_second'] = self.metrics['pairs'] / max(now-began, 1e-9)
                    self.metrics['max_pair_latency_ms'] = max(self.metrics['max_pair_latency_ms'], (now-start)*1000)
                    if last is not None:
                        self.metrics['max_completion_gap_ms'] = max(self.metrics['max_completion_gap_ms'], (now-last)*1000)
                last = now
                seq += 1
                self.stop.wait(max(0, 1/self.rate-(time.monotonic()-start)))
            if select.select(sockets, [], [], 0.05)[0]:
                raise RuntimeError('trailing/duplicate frame')
        except Exception as exc:
            self.error = str(exc)
        finally:
            for sock in sockets: sock.close()

    def finish(self):
        self.stop.set()
        self.thread.join(3)
        if self.thread.is_alive():
            raise RuntimeError('traffic worker did not stop')
        if self.error:
            raise RuntimeError(self.error)


def phase(traffic, seconds=2):
    before = traffic.snapshot()['pairs']
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        if traffic.error: raise RuntimeError(traffic.error)
        time.sleep(0.02)
    result = traffic.snapshot()
    result['pairs_in_phase'] = result['pairs']-before
    if result['pairs_in_phase'] < 1:
        raise RuntimeError('no traffic progress in phase')
    return result


def check_state(status, active):
    for c, expected in zip(status['channels'], active):
        if c['active'] != expected or (expected and c['bitrate'] != 500000) or (expected and c.get('stopped_reason')):
            raise RuntimeError(f'unexpected channel state: {status}')


def check_counters(before, after, survivor, expected):
    for i in (0, 1):
        a, b = before['channels'][i]['counters'], after['channels'][i]['counters']
        for key in ('tag_2', 'hardware_overrun_events', 'tx_orphan_receipts',
                    'tx_rejected_format', 'tx_cancelled'):
            if b.get(key, 0) != a.get(key, 0):
                raise RuntimeError(f'channel {i+1}: {key} increased')
    i = survivor-1
    actual = after['channels'][i]['counters'].get('tx_confirmed', 0)-before['channels'][i]['counters'].get('tx_confirmed', 0)
    if actual != expected:
        raise RuntimeError(f'channel {survivor}: TX confirmations {actual}, expected {expected}')
    return actual


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--peer', default='can0')
    parser.add_argument('--socket', default='/run/cancasexl/control.sock')
    parser.add_argument('--report', default='socketcan_independent_report.json')
    args = parser.parse_args(argv)
    report = dict(started=time.time(), peer=args.peer, bitrate=500000,
                  cases=[], passed=False, cleanup_errors=[])
    connected = False
    result = 1
    with open(args.report, 'x', encoding='utf-8') as output:
        try:
            initial = control(args.socket, action='status')
            if initial.get('mode') != 'userspace-vxcan-bridge':
                raise RuntimeError('unexpected control service')
            names = [c['interface'] for c in initial['channels']]
            if args.peer in names: raise ValueError('peer must be an external CAN adapter')
            peer_info = json.loads(subprocess.check_output(
                ['ip', '-j', '-details', 'link', 'show', 'dev', args.peer], text=True))
            validate_peer(peer_info, args.peer)
            report['peer_before'] = peer_info
            connected = True
            for ch in (1, 2): control(args.socket, action='down', channel=ch)
            for ch in (1, 2): control(args.socket, action='up', channel=ch, bitrate=500000)
            for survivor in (1, 2):
                target = 3-survivor
                case = dict(survivor=survivor, stopped_channel=target, passed=False, phases={})
                report['cases'].append(case)
                traffic = Traffic(names[survivor-1], args.peer)
                before = control(args.socket, action='status')
                check_state(before, [True, True])
                print(f'Channel {survivor} exchanges with {args.peer}; stopping/restarting channel {target}', flush=True)
                traffic.thread.start()
                try:
                    case['phases']['both_up'] = phase(traffic)
                    control(args.socket, action='down', channel=target)
                    status = control(args.socket, action='status')
                    expected = [True, True]; expected[target-1] = False
                    check_state(status, expected)
                    case['status_target_down'] = status
                    case['phases']['target_down'] = phase(traffic)
                    control(args.socket, action='up', channel=target, bitrate=500000)
                    status = control(args.socket, action='status')
                    check_state(status, [True, True])
                    case['status_target_restarted'] = status
                    case['phases']['target_restarted'] = phase(traffic)
                finally:
                    try: traffic.finish()
                    finally: case['traffic'] = traffic.snapshot()
                # Give the final physical receipt time to arrive after peer RX.
                deadline = time.monotonic()+2
                while True:
                    after = control(args.socket, action='status')
                    i = survivor-1
                    actual = after['channels'][i]['counters'].get('tx_confirmed', 0)-before['channels'][i]['counters'].get('tx_confirmed', 0)
                    if actual >= case['traffic']['pairs'] or time.monotonic() >= deadline: break
                    time.sleep(0.02)
                check_state(after, [True, True])
                case['tx_confirmed'] = check_counters(before, after, survivor, case['traffic']['pairs'])
                case['passed'] = True
                print(f'PASS channel {survivor}: {case["traffic"]["pairs"]} pairs', flush=True)
            # After the second restart verify channel 1 again (channel 2 was
            # already exercised as survivor after its restart in the first case).
            case = dict(name='final_channel1_restart_check', passed=False)
            report['cases'].append(case)
            traffic = Traffic(names[0], args.peer)
            before = control(args.socket, action='status')
            traffic.thread.start()
            try: case['traffic'] = phase(traffic, 1)
            finally:
                try: traffic.finish()
                finally: case['traffic'] = traffic.snapshot()
            time.sleep(0.1)
            after = control(args.socket, action='status')
            check_state(after, [True, True])
            case['tx_confirmed'] = check_counters(before, after, 1, case['traffic']['pairs'])
            case['passed'] = True
            report['passed'] = True
            result = 0
        except (Exception, KeyboardInterrupt) as exc:
            report['error'] = str(exc) or 'interrupted'
            print('Independent test: '+report['error'], flush=True)
        finally:
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
    print('Report:', Path(args.report).resolve(), flush=True)
    return result


if __name__ == '__main__':
    raise SystemExit(main())
