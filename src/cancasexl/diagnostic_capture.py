"""Bounded diagnostic summaries; no interpretation of proprietary state bytes."""
from collections import deque
import re


class ErrorSummary:
    def __init__(self, limit=128):
        self.limit = limit
        self.groups = {}
        self.tail = deque(maxlen=32)
        self.total = 0
        self.bus_errors = 0
        self.state_changes = 0
        self.untracked = 0

    def add(self, event, timestamp):
        self.total += 1
        identifier = int(event['can_id'], 16)
        self.bus_errors += bool(identifier & 0x80)
        self.state_changes += bool(identifier & 0x04)
        event = dict(event, observed_at=timestamp)
        self.tail.append(event)
        key = (event['can_id'], event['data'])
        if key not in self.groups:
            if len(self.groups) >= self.limit:
                self.untracked += 1
                return
            self.groups[key] = dict(can_id=key[0], data=key[1], count=0,
                                    first_observed_at=timestamp, last_observed_at=timestamp)
        group = self.groups[key]
        group['count'] += 1
        group['last_observed_at'] = timestamp

    def report(self):
        return dict(error_frame_count=self.total, bus_error_frame_count=self.bus_errors,
                    state_change_frame_count=self.state_changes, error_groups=list(self.groups.values()),
                    error_groups_untracked=self.untracked, error_frames_tail=list(self.tail),
                    error_tail_is_sample=self.total > len(self.tail))


def kernel_capture(lines, channel):
    marker = f'diagnostic ch{channel} '
    starts = [i for i, line in enumerate(lines) if marker+'begin' in line]
    if not starts:
        return dict(complete=False, reason='diagnostic begin marker missing', lines=[])
    selected = [line for line in lines[starts[-1]:] if marker in line]
    summaries = [re.search(r'summary states=(\d+) state-dropped=(\d+) error-untracked=(\d+)', line)
                 for line in selected]
    summaries = [m for m in summaries if m]
    ended = any('window ended; stopped' in line for line in selected)
    states = [line for line in selected if ' state-record=' in line]
    result = dict(lines=selected, state_records=states, window_ended=ended, complete=False)
    if summaries:
        count, dropped, untracked = map(int, summaries[-1].groups())
        result.update(states_total=count, state_records_dropped=dropped, error_records_untracked=untracked)
        result['complete'] = ended and dropped == 0 and untracked == 0 and len(states) == count
    return result
