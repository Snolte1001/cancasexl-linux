"""Experimental two-channel SocketCAN bridge using owned vxcan pairs.

Kernel sockets carry Classical CAN data. Physical configuration/confirmation is
owned by this daemon, NOT by the virtual CAN netdevices. See TESTING.md.
"""
import argparse
import errno
from collections import Counter, deque
import fcntl
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import struct
import subprocess
import sys
import time

from .driver import CANFrame
from .native import ExperimentalDriver, NativeUSB
from .wire import decode_can_error

RATES = (125000, 250000, 500000, 1000000)
FRAME = struct.Struct('=IBBBB8s')
EFF, RTR, ERR = 0x80000000, 0x40000000, 0x20000000
SO_RXQ_OVFL = 40


def unpack_frame(raw):
    if len(raw) != 16:
        raise ValueError('only Classical CAN frames (16 bytes) supported')
    ident, length, pad, res, len8, data = FRAME.unpack(raw)
    if ident & ERR:
        raise ValueError('injected error frames are unsupported')
    if length > 8 or len8:
        raise ValueError('DLC must be 0..8; len8_dlc unsupported')
    return CANFrame(ident & 0x1fffffff, b'' if ident & RTR else data[:length],
                    bool(ident & EFF), bool(ident & RTR), length)


def pack_frame(frame):
    return FRAME.pack(frame.arbitration_id | (EFF if frame.extended else 0) | (RTR if frame.remote else 0),
                      frame.dlc, 0, 0, 0, frame.data.ljust(8, b'\0'))


class ChannelFault(RuntimeError):
    def __init__(self, channel, message):
        self.channel = channel
        super().__init__(message)


class Engine:
    """One USB owner, one outstanding physical TX per channel, bounded queues."""
    def __init__(self, driver, emit, log, queue_limit=256, tx_timeout=5.0):
        self.driver, self.emit, self.log = driver, emit, log
        self.queue_limit, self.tx_timeout = queue_limit, tx_timeout
        self.queues = [deque(), deque()]
        self.pending = [None, None]
        self.stats = [Counter(), Counter()]

    def submit(self, channel, raw):
        if channel not in self.driver.started:
            self.stats[channel]['tx_rejected_down'] += 1
            return
        try:
            frame = unpack_frame(raw)
        except ValueError as exc:
            self.stats[channel]['tx_rejected_format'] += 1
            self.log('rejected', channel=channel + 1, reason=str(exc))
            return
        if len(self.queues[channel]) >= self.queue_limit:
            raise ChannelFault(channel, 'TX queue full; stopping this channel')
        self.queues[channel].append(frame)
        self.stats[channel]['tx_queued'] += 1

    def pump(self, now):
        # Both channels submit before we wait for USB receipts.
        for channel in (0, 1):
            if channel not in self.driver.started:
                continue
            job = self.pending[channel]
            if job is not None:
                if now - job[1] >= self.tx_timeout:
                    raise ChannelFault(channel, 'TX receipt timeout (not proof of bus-off)')
            elif self.queues[channel]:
                frame = self.queues[channel].popleft()
                self.driver.send(channel, frame)
                self.pending[channel] = (frame, now)
                self.stats[channel]['tx_usb_submitted'] += 1
                self.log('tx_submit', channel=channel+1, id=frame.arbitration_id,
                         extended=frame.extended, data=frame.data.hex())

    def event(self, event):
        channel = event.channel_index
        if channel not in (0, 1):
            self.log('unknown_channel', raw=event.raw.hex())
            return
        if event.hardware_overrun:
            self.stats[channel]['hardware_overrun_events'] += 1
            raise ChannelFault(channel, 'hardware RX overrun; data lost')
        if event.tag == 1:
            frame = event.can_frame
            if event.direction == 'tx_receipt':
                job = self.pending[channel]
                if job is None:
                    self.stats[channel]['tx_orphan_receipts'] += 1
                    self.log('orphan_receipt', channel=channel+1, raw=event.raw.hex())
                elif frame != job[0]:
                    raise ChannelFault(channel, 'TX receipt does not match outstanding frame')
                else:
                    self.pending[channel] = None
                    self.stats[channel]['tx_confirmed'] += 1
                    # Do NOT inject receipts as RX: SocketCAN already echoed TX.
                    self.log('tx_confirmed', channel=channel+1, raw=event.raw.hex())
            elif channel in self.driver.started:
                self.emit(channel, pack_frame(frame))
                self.stats[channel]['rx_forwarded'] += 1
                self.log('rx', channel=channel+1, raw=event.raw.hex())
        elif event.tag in (2, 4, 5, 14):
            self.stats[channel][f'tag_{event.tag}'] += 1
            fields = dict(channel=channel+1, tag=event.tag, raw=event.raw.hex())
            error = decode_can_error(event)
            if error:
                fields.update(ecc=error.ecc, direction=error.direction,
                              error_type=error.error_type, segment=error.segment)
            self.log('hardware_event', **fields)

    def cancel(self, channel):
        self.stats[channel]['tx_cancelled'] += len(self.queues[channel]) + int(self.pending[channel] is not None)
        self.queues[channel].clear()
        self.pending[channel] = None

    def status(self):
        return [dict(channel=c+1, active=c in self.driver.started,
                     bitrate=self.driver.bitrates.get(c), queued=len(self.queues[c]),
                     outstanding=self.pending[c] is not None, counters=dict(self.stats[c]))
                for c in (0, 1)]


def ip(*args):
    result = subprocess.run(['ip', *args], capture_output=True, text=True, timeout=5)
    if result.returncode:
        raise RuntimeError('ip ' + ' '.join(args) + ': ' + result.stderr.strip())
    return result.stdout


class Links:
    """Create only new interfaces; never reuse/delete someone else's links."""
    def __init__(self, names):
        self.names = names
        self.peers = [name + 'p' for name in names]
        self.created = []
        self.sockets = []
        self.drop_counts = [0, 0]
        all_names = self.names + self.peers
        if len(set(all_names)) != 4 or any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,13}', n) for n in all_names):
            raise ValueError('interface names must be distinct, alphanumeric/underscore, at most 13 chars for public names')

    def open(self):
        existing = {name for index, name in socket.if_nameindex()}
        if existing.intersection(self.names + self.peers):
            raise RuntimeError('interface name already exists; choose --interfaces with unused names')
        try:
            for name, peer in zip(self.names, self.peers):
                ip('link', 'add', name, 'type', 'vxcan', 'peer', 'name', peer)
                self.created.append((name, socket.if_nametoindex(name)))
                for link in (name, peer):
                    ip('link', 'set', 'dev', link, 'mtu', '16')
                sock = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
                self.sockets.append(sock)
                sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_RECV_OWN_MSGS, 0)
                sock.setsockopt(socket.SOL_SOCKET, SO_RXQ_OVFL, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
                sock.bind((peer,))
                sock.setblocking(False)
                ip('link', 'set', 'dev', peer, 'up')
        except BaseException:
            self.close()
            raise

    def is_up(self, channel):
        request = struct.pack('16sH22x', self.names[channel].encode(), 0)
        reply = fcntl.ioctl(self.sockets[channel].fileno(), 0x8913, request)
        return bool(struct.unpack_from('H', reply, 16)[0] & 1)

    def set_up(self, channel, up):
        ip('link', 'set', 'dev', self.names[channel], 'up' if up else 'down')

    def receive(self, channel):
        try:
            raw, anc, flags, addr = self.sockets[channel].recvmsg(72, socket.CMSG_SPACE(4))
        except BlockingIOError:
            return None
        except OSError as exc:
            # A stopped vxcan pair can report ENETDOWN rather than EAGAIN.
            # This is expected during --start-down and while draining on stop.
            if exc.errno == errno.ENETDOWN and not self.is_up(channel):
                return None
            raise
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            raise ChannelFault(channel, 'truncated SocketCAN datagram')
        for level, kind, data in anc:
            if level == socket.SOL_SOCKET and kind == SO_RXQ_OVFL:
                count = struct.unpack('=I', data)[0]
                if count != self.drop_counts[channel]:
                    self.drop_counts[channel] = count
                    raise ChannelFault(channel, 'SocketCAN receive queue overflow; data lost')
        return raw

    def discard(self, channel):
        # Public interface must be DOWN: no new application frames can enter.
        while self.receive(channel) is not None:
            pass

    def emit(self, channel, raw):
        try:
            if self.sockets[channel].send(raw) != len(raw):
                raise OSError('short SocketCAN write')
        except OSError as exc:
            raise ChannelFault(channel, 'SocketCAN RX forwarding failed: ' + str(exc)) from exc

    def close(self):
        for sock in self.sockets:
            sock.close()
        self.sockets.clear()
        errors = []
        for name, index in reversed(self.created):
            try:
                try:
                    current = socket.if_nametoindex(name)
                except OSError:
                    continue
                if current != index:
                    errors.append(f'{name} was replaced externally; not deleting it')
                else:
                    ip('link', 'delete', 'dev', name)
            except Exception as exc:
                errors.append(str(exc))
        self.created.clear()
        if errors:
            raise RuntimeError('; '.join(errors))


class Bridge:
    def __init__(self, driver, links, log, queue_limit, tx_timeout):
        self.driver, self.links, self.log = driver, links, log
        self.engine = Engine(driver, links.emit, log, queue_limit, tx_timeout)
        self.running = True
        self.reasons = [None, None]

    def stop(self, channel, reason='requested'):
        self.links.set_up(channel, False)
        if channel in self.driver.started:
            self.driver.stop_channel(channel)
        self.engine.cancel(channel)
        self.drain()
        self.links.discard(channel)
        self.reasons[channel] = reason
        self.log('channel_down', channel=channel+1, reason=reason)

    def drain(self):
        # Remove buffered receipts before allowing the next start. Other channel
        # events continue to be handled, with a bounded drain to avoid starvation.
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            event = self.driver.receive(timeout_ms=1)
            if event is None:
                break
            self.engine.event(event)
        else:
            raise RuntimeError('USB event stream did not drain after channel stop')

    def start(self, channel, bitrate):
        if bitrate not in RATES:
            raise ValueError('supported bitrates: 125000, 250000, 500000, 1000000')
        if channel in self.driver.started:
            if self.driver.bitrates[channel] == bitrate:
                return
            raise ValueError('stop this channel before changing its bitrate')
        # Also used after an external ip link up: close the ingress during setup.
        self.links.set_up(channel, False)
        self.links.discard(channel)
        self.drain()
        self.driver.start_channel(channel, bitrate)
        self.links.set_up(channel, True)
        self.reasons[channel] = None
        self.log('channel_up', channel=channel+1, bitrate=bitrate)

    def status(self):
        channels = self.engine.status()
        for c, row in enumerate(channels):
            row.update(interface=self.links.names[c], stopped_reason=self.reasons[c])
        return dict(ok=True, mode='userspace-vxcan-bridge', channels=channels,
                    hardware_busoff_verified=False, socketcan_echo_is_hardware_ack=False)

    def control(self, request):
        action = request.get('action')
        if action == 'status':
            return self.status()
        if action == 'shutdown':
            self.running = False
            return dict(ok=True, message='shutdown requested')
        channel = request.get('channel')
        if type(channel) is not int or channel not in (1, 2):
            raise ValueError('channel must be 1 or 2')
        c = channel - 1
        if action == 'down':
            self.stop(c)
        elif action == 'up':
            self.start(c, request.get('bitrate', self.driver.bitrates[c]))
        else:
            raise ValueError('unknown action')
        return self.status()

    def step(self):
        try:
            for c in (0, 1):
                up = self.links.is_up(c)
                if not up and c in self.driver.started:
                    self.stop(c, 'ip link down')
                elif up and c not in self.driver.started:
                    self.start(c, self.driver.bitrates[c])
                # A bounded batch prevents one busy channel starving the other.
                for _ in range(16):
                    raw = self.links.receive(c)
                    if raw is None:
                        break
                    self.engine.submit(c, raw)
            self.engine.pump(time.monotonic())
            for _ in range(64):
                event = self.driver.receive(timeout_ms=1)
                if event is None:
                    break
                self.engine.event(event)
        except ChannelFault as exc:
            self.log('channel_fault', channel=exc.channel+1, reason=str(exc))
            self.stop(exc.channel, str(exc))


def serve_control(server, bridge):
    try:
        client, _ = server.accept()
    except BlockingIOError:
        return
    with client:
        client.settimeout(0.1)
        try:
            raw, anc, flags, addr = client.recvmsg(4096)
            if flags & socket.MSG_TRUNC:
                raise ValueError('control request too large')
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError('expected JSON object')
            result = bridge.control(request)
        except (ValueError, socket.timeout) as exc:
            result = dict(ok=False, error=str(exc))
        # Hardware/USB failures deliberately escape: stop and clean up the daemon.
        try:
            client.sendall(json.dumps(result).encode())
        except (BrokenPipeError, socket.timeout):
            pass


def control_main(argv=None):
    parser = argparse.ArgumentParser(description='Control the CANcase XL SocketCAN bridge')
    parser.add_argument('--socket', default='/run/cancasexl/control.sock')
    parser.add_argument('action', choices=('status', 'up', 'down', 'shutdown'))
    parser.add_argument('--channel', type=int, choices=(1, 2))
    parser.add_argument('--bitrate', type=int, choices=RATES)
    args = parser.parse_args(argv)
    if args.action in ('up', 'down') and args.channel is None:
        parser.error('--channel is required for up/down')
    request = dict(action=args.action)
    if args.channel is not None:
        request['channel'] = args.channel
    if args.bitrate is not None:
        request['bitrate'] = args.bitrate
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as sock:
            sock.settimeout(15)
            sock.connect(args.socket)
            sock.sendall(json.dumps(request).encode())
            result = json.loads(sock.recv(16384))
        print(json.dumps(result, indent=2))
        return 0 if result.get('ok') else 1
    except (OSError, ValueError) as exc:
        print('CANcase XL control: ' + str(exc), file=sys.stderr)
        return 1


def final_status(bridge, error, cleanup_errors):
    """Report daemon termination without claiming physical stop on USB failure."""
    previous = bridge.status()
    for c in (0, 1):
        bridge.engine.cancel(c)
    status = bridge.status()
    reason = 'bridge_error' if error else ('cleanup_error' if cleanup_errors else 'shutdown')
    for row in status['channels']:
        row.update(active=False, stopped_reason=reason)
    status.update(ok=not error and not cleanup_errors, running=False,
                  error=error, cleanup_errors=cleanup_errors,
                  hardware_state='unknown' if error or cleanup_errors else 'stopped',
                  status_before_cleanup=previous)
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bus', required=True, type=int)
    parser.add_argument('--address', required=True, type=int)
    parser.add_argument('--driver', help='own original vcasexl.sys 8.7.16.0')
    parser.add_argument('--interfaces', nargs=2, default=['canxl0', 'canxl1'])
    parser.add_argument('--bitrate-ch1', type=int, choices=RATES, default=500000)
    parser.add_argument('--bitrate-ch2', type=int, choices=RATES, default=500000)
    parser.add_argument('--start-down', action='store_true', help='initialize with both physical channels stopped')
    parser.add_argument('--queue-limit', type=int, default=256)
    parser.add_argument('--tx-timeout', type=float, default=5.0)
    parser.add_argument('--socket', default='/run/cancasexl/control.sock')
    parser.add_argument('--log', default='socketcan_events.jsonl')
    args = parser.parse_args(argv)
    if not 1 <= args.queue_limit <= 65536 or not math.isfinite(args.tx_timeout) or args.tx_timeout <= 0:
        parser.error('queue-limit must be 1..65536 and tx-timeout finite and positive')
    if sys.platform != 'linux' or os.geteuid() != 0:
        parser.error('run on Linux with sudo (USB access and CAP_NET_ADMIN required)')
    links = None
    driver = None
    bridge = None
    server = None
    lock = None
    bound = False
    output = None
    result = 0
    failure = None
    cleanup_errors = []
    def stop_signal(signum, frame):
        if bridge is None:
            raise KeyboardInterrupt
        bridge.running = False
    signal.signal(signal.SIGTERM, stop_signal)
    signal.signal(signal.SIGINT, stop_signal)
    try:
        links = Links(args.interfaces)
        path = Path(args.socket)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        meta = path.parent.stat()
        if path.parent.is_symlink() or meta.st_uid != 0 or meta.st_mode & 0o022:
            raise ValueError('control directory must be root-owned, non-symlink and not group/world-writable')
        # Serialize even when an alternative control socket name is chosen.
        lockpath = Path('/run') / f'cancasexl-usb-{args.bus}-{args.address}.lock'
        lock = os.fdopen(os.open(lockpath, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'w')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        server.bind(str(path))  # Existing sockets are never silently removed.
        bound = True
        os.chmod(path, 0o600)
        server.listen(4)
        server.setblocking(False)
        output = open(args.log, 'x', encoding='utf-8')
        def log(kind, **fields):
            output.write(json.dumps(dict(time=time.time(), event=kind, **fields)) + '\n')
            output.flush()
        log('startup', interfaces=args.interfaces, rates=[args.bitrate_ch1, args.bitrate_ch2])
        links.open()  # Probe vxcan support BEFORE firmware or CAN activation.
        driver = ExperimentalDriver(NativeUSB(args.bus, args.address))
        driver.open(driver_path=args.driver,
                    channel_bitrates=(args.bitrate_ch1, args.bitrate_ch2), start_channels=())
        bridge = Bridge(driver, links, log, args.queue_limit, args.tx_timeout)
        if not args.start_down:
            for c, rate in enumerate((args.bitrate_ch1, args.bitrate_ch2)):
                bridge.start(c, rate)
        print(json.dumps(bridge.status(), indent=2), flush=True)
        while bridge.running:
            serve_control(server, bridge)
            if bridge.running:
                bridge.step()
    except KeyboardInterrupt:
        result = 130
    except Exception as exc:
        failure = str(exc)
        print('CANcase XL bridge stopped: ' + failure, file=sys.stderr)
        result = 1
    finally:
        # Remove ingress first; keep the log open until cleanup is complete.
        before_cleanup = bridge.status() if bridge else None
        for name, resource in (('links', links), ('driver', driver),
                               ('server', server), ('lock', lock)):
            if resource is not None:
                try:
                    resource.close()
                except Exception as exc:
                    message = name + ': ' + str(exc)
                    cleanup_errors.append(message)
                    print('Cleanup incomplete: ' + message, file=sys.stderr)
                    result = 1
        if bound:
            try:
                Path(args.socket).unlink(missing_ok=True)
            except Exception as exc:
                cleanup_errors.append('control socket: ' + str(exc))
                result = 1
        if output:
            try:
                if bridge:
                    status = final_status(bridge, failure, cleanup_errors)
                    status['status_before_cleanup'] = before_cleanup
                else:
                    status = dict(ok=False, running=False, error=failure,
                                  cleanup_errors=cleanup_errors)
                log('final_status', **status)
            except Exception as exc:
                print('Final log failed: ' + str(exc), file=sys.stderr)
                result = 1
            finally:
                output.close()
    return result


if __name__ == '__main__':
    raise SystemExit(main())
