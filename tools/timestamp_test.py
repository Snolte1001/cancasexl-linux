#!/usr/bin/env python3
"""Check raw RX, local echo and TX error-queue hardware timestamps on two wired channels."""
import argparse
import socket
import struct
import time

SO_TIMESTAMPING_NEW = 65
STAMP_FLAGS = (1 << 0) | (1 << 2) | (1 << 6)  # TX_HW, RX_HW, RAW_HW
FRAME = struct.Struct('=IB3x8s')
# CAN_ERR_FLAG in a receive mask selects the error-only receiver list.
DATA_FILTER_MASK = 0xC00007FF  # EFF and RTR flags plus the 11-bit identifier


def timestamp(ancillary):
    for level, kind, value in ancillary:
        if level == socket.SOL_SOCKET and kind == SO_TIMESTAMPING_NEW:
            if len(value) != 48:
                raise RuntimeError('Unexpected SCM_TIMESTAMPING_NEW size')
            fields = struct.unpack('=6q', value)
            sec, ns = fields[4:6]
            if sec < 0 or not 0 <= ns < 1_000_000_000:
                raise RuntimeError('Invalid raw hardware timestamp')
            raw = sec * 1_000_000_000 + ns
            if not raw:
                raise RuntimeError('Missing raw hardware timestamp (zero)')
            return raw
    raise RuntimeError('SCM_TIMESTAMPING_NEW missing; no software fallback accepted')


def open_socket(name):
    sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    try:
        sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_RECV_OWN_MSGS, 1)
        sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPING_NEW, STAMP_FLAGS)
        # Only this test's standard data identifiers; reject unrelated traffic.
        sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER,
                        b''.join(struct.pack('=II', i, DATA_FILTER_MASK) for i in (0x650, 0x651)))
        sock.bind((name,))
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise


def run(names, count):
    sockets = []
    previous = {}
    deltas = []
    try:
        sockets = [open_socket(names[0])]
        sockets.append(open_socket(names[1]))
        for seq in range(count):
            for sender in (0, 1):
                frame = FRAME.pack(0x650 + sender, 8, struct.pack('=Q', seq))
                if sockets[sender].send(frame) != len(frame):
                    raise RuntimeError('Short CAN send')
                found = {}
                deadline = time.monotonic() + 2
                while len(found) != 3:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f'Timestamp timeout: received {sorted(found)}')
                    for i, sock in enumerate(sockets):
                        kinds = [('echo' if i == sender else 'rx', 0)]
                        if i == sender:
                            kinds.append(('tx', socket.MSG_ERRQUEUE))
                        for kind, flags in kinds:
                            try:
                                data, anc, msgflags, _ = sock.recvmsg(128, 512, flags | socket.MSG_DONTWAIT)
                            except BlockingIOError:
                                continue
                            if msgflags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
                                raise RuntimeError('Truncated CAN message or timestamp metadata')
                            if data != frame:
                                raise RuntimeError('Unexpected CAN frame; stop other traffic using test IDs')
                            if kind in found:
                                raise RuntimeError('Duplicate receipt')
                            stamp = timestamp(anc)
                            key = (sender, kind)
                            if key in previous and stamp <= previous[key]:
                                raise RuntimeError('Device time did not increase; possible reset or wrap')
                            previous[key] = stamp
                            found[kind] = stamp
                    if len(found) != 3:
                        time.sleep(0.001)
                if found['echo'] != found['tx']:
                    raise RuntimeError('TX error-queue stamp differs from local echo stamp')
                deltas.append(found['tx'] - found['rx'])
                time.sleep(0.01)
        print(f'PASS: {count * 2} frames; raw RX, TX echo and TX error-queue stamps verified.')
        print(f'TX minus peer RX: {min(deltas)} .. {max(deltas)} ns (same-device clock only).')
    finally:
        for sock in sockets:
            sock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interfaces', nargs=2, required=True)
    parser.add_argument('--count', type=int, default=20)
    args = parser.parse_args()
    if args.interfaces[0] == args.interfaces[1] or not 1 <= args.count <= 1000:
        parser.error('Use two distinct interfaces and count 1..1000')
    try:
        run(args.interfaces, args.count)
    except (OSError, RuntimeError) as exc:
        parser.exit(1, f'FAIL: {exc}\n')


if __name__ == '__main__':
    main()
