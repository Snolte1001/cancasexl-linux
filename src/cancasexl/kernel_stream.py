"""Bounded pipelined CAN traffic; eight-byte standard frames with full sequence."""
from collections import deque
import errno
import secrets
import select
import time
from .socketcan_independent import Traffic, open_can, traffic_frames
from .socketcan import pack_frame, unpack_frame


def next_deadline(previous, now, period, window):
    """Absolute cadence, with bounded catch-up after a stall."""
    deadline = previous + period
    skipped = max(0, int((now-deadline)/period) - (window-1))
    return deadline + skipped*period, skipped


class StreamTraffic(Traffic):
    def __init__(self, vector, peer, *, rate=100, profile='standard', window=4):
        super().__init__(vector, peer, rate=rate, profile=profile)
        if profile != 'standard':
            raise ValueError('stream traffic requires standard 8-byte sequence frames')
        if not 1 <= window <= 8:
            raise ValueError('window must be between 1 and 8')
        self.metrics.pop('max_pair_latency_ms')  # Not a request/response test.
        self.window = window
        self.metrics.update(max_outstanding=[0, 0], backpressure_retries=[0, 0],
                            submitted_fps=[0, 0], received_fps=[0, 0],
                            max_receive_latency_ms=[0, 0], drain_seconds=0, skipped_schedule_slots=[0, 0], max_send_lateness_ms=[0, 0])

    def run(self):
        sockets = []
        try:
            sockets = [open_can(self.names[0], 0x652)]
            sockets.append(open_can(self.names[1], 0x651))
            for sock in sockets: sock.setblocking(False)
            token = secrets.randbits(32)
            pending = [deque(), deque()]  # Indexed by sending socket.
            sent, received = [0, 0], [0, 0]  # Both indexed by sender internally.
            began = time.monotonic()
            due = [began, began]
            progress = [began, began]
            retry_at = [began, began]
            stop_at = None
            target = None
            last_pair = None
            while True:
                now = time.monotonic()
                if self.stop.is_set() and stop_at is None:
                    stop_at, target = now, max(sent)
                if stop_at is not None and now-stop_at > 2:
                    raise TimeoutError(f'drain timeout: sent={sent}, received={received}')
                # No wait for a pair's return before submitting the next one.
                # Keep directions balanced, but allow up to window unreceived frames.
                for i, sock in enumerate(sockets):
                    allowed = sent[i] < target if target is not None else sent[i] <= sent[1-i]
                    if not allowed or len(pending[i]) >= self.window or now < max(due[i], retry_at[i]): continue
                    frame = traffic_frames(token, sent[i])[i]
                    wire = pack_frame(frame)
                    try:
                        count = sock.send(wire)
                    except OSError as exc:
                        if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.ENOBUFS): raise
                        retry_at[i] = now+0.0005
                        with self.lock: self.metrics['backpressure_retries'][i] += 1
                        continue  # Same sequence retried, never silently skipped.
                    if count != len(wire): raise RuntimeError('short CAN datagram send')
                    pending[i].append((frame, now))
                    sent[i] += 1
                    lateness = max(0, now-due[i])*1000
                    due[i], skipped = next_deadline(due[i], now, 1/self.rate, self.window)
                    with self.lock:
                        self.metrics['skipped_schedule_slots'][i] += skipped
                        self.metrics['max_send_lateness_ms'][i] = max(self.metrics['max_send_lateness_ms'][i], lateness)
                        self.metrics['submitted'][i] = sent[i]
                        self.metrics['max_outstanding'][i] = max(self.metrics['max_outstanding'][i], len(pending[i]))
                now = time.monotonic()
                deadlines = [max(due[i], retry_at[i]) for i in (0, 1)
                             if len(pending[i]) < self.window and
                             (sent[i] < target if target is not None else sent[i] <= sent[1-i])]
                timeout = min(0.01, max(0, min(deadlines)-now)) if deadlines else 0.01
                ready = select.select(sockets, [], [], timeout)[0]
                for sock in ready:
                    destination = sockets.index(sock)
                    source = 1-destination
                    # Bound receive work so sends and stop requests stay responsive.
                    for _ in range(64):
                        try: wire = sock.recv(72)
                        except BlockingIOError: break
                        frame = unpack_frame(wire)
                        if not pending[source] or frame != pending[source][0][0]:
                            raise RuntimeError(f'{self.names[destination]}: unexpected, duplicate or out-of-order frame at sequence {received[source]}')
                        _, submitted_at = pending[source].popleft()
                        received[source] += 1
                        now = time.monotonic()
                        progress[source] = now
                        with self.lock:
                            m = self.metrics
                            m['received'][destination] = received[source]
                            m['max_receive_latency_ms'][destination] = max(m['max_receive_latency_ms'][destination], (now-submitted_at)*1000)
                            pairs = min(received)
                            if pairs > m['pairs']:
                                m['pairs'] = pairs
                                m['payload_bytes'] = pairs*8
                                m['variants'] = {'standard_dlc8': pairs}
                                if last_pair is not None:
                                    m['max_completion_gap_ms'] = max(m['max_completion_gap_ms'], (now-last_pair)*1000)
                                last_pair = now
                now = time.monotonic()
                elapsed = max(now-began, 1e-9)
                with self.lock:
                    self.metrics['elapsed_seconds'] = elapsed
                    self.metrics['achieved_pairs_per_second'] = min(received)/elapsed
                    self.metrics['submitted_fps'] = [n/elapsed for n in sent]
                    self.metrics['received_fps'] = [received[1-i]/elapsed for i in (0, 1)]
                for i in (0, 1):
                    if pending[i] and now-pending[i][0][1] > 1:
                        raise TimeoutError(f'direction {i}: oldest frame unreceived for 1 second')
                    if target is None and now-progress[i] > max(2, 2/self.rate):
                        raise TimeoutError(f'direction {i}: no receive progress')
                if target is not None and sent == [target, target] and not any(pending): break
            with self.lock: self.metrics['drain_seconds'] = time.monotonic()-stop_at
            if select.select(sockets, [], [], 0.05)[0]:
                raise RuntimeError('trailing/duplicate frame')
        except Exception as exc:
            self.error = str(exc)
        finally:
            for sock in sockets: sock.close()
