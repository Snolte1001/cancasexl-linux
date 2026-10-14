"""Capture-derived Classical CAN records for both CANcase XL channels.
RX endpoint 0x82: uint32 count + 24-byte records. TX endpoints 0x03/0x04.
RTR flag 0x04 verified against USBPcap and CANalyzer BLF, 2026-10-14.
"""
from dataclasses import dataclass
import struct
from .driver import CANFrame

RECORD = struct.Struct('<BBH6sIBB8s')

@dataclass(frozen=True)
class WireEvent:
    tag: int
    channel: int
    transaction: int
    timestamp_ticks: int
    identifier_raw: int
    dlc_raw: int
    flags_raw: int
    storage: bytes
    raw: bytes

    @property
    def channel_index(self):
        return self.channel & 0x7f

    @property
    def hardware_overrun(self):
        return bool(self.channel & 0x80)

    @property
    def can_frame(self):
        # Timer/chip-state/flush events contain stale union bytes; never decode
        # them as CAN messages even when known payload patterns are present.
        if self.tag != 1:
            return None
        if self.channel_index not in (0, 1):
            raise ValueError('unobserved CAN channel')
        if self.flags_raw & ~0x05:
            raise ValueError('unverified CAN flags; retain raw event')
        extended = bool(self.identifier_raw & 0x80000000)
        identifier = self.identifier_raw & 0x7FFFFFFF
        return CANFrame(identifier, b'' if self.flags_raw & 4 else self.storage[:self.dlc_raw],
                        extended=extended, remote=bool(self.flags_raw & 4), dlc=self.dlc_raw)

    @property
    def direction(self):
        if self.tag != 1:
            return None
        if self.flags_raw & ~0x05:
            return 'unknown'
        return 'tx_receipt' if self.flags_raw & 1 else 'rx'

def decode_record(raw):
    if len(raw) != RECORD.size:
        raise ValueError('CANcase event must be exactly 24 bytes')
    tag, channel, transaction, timestamp, identifier, dlc, flags, data = RECORD.unpack(raw)
    return WireEvent(tag, channel, transaction, int.from_bytes(timestamp, 'little'),
                     identifier, dlc, flags, data, raw)

def decode_receive_transfer(payload):
    """Compact count/records, or captured 512-byte full blocks plus compact tail.

    A full block holds count=21, 504 record bytes and four zero padding bytes.
    Multiple blocks can share one URB. No reassembly across separate transfers
    or recovery by searching for plausible record headers is attempted.
    """
    if len(payload) < 4:
        raise ValueError('missing event count')
    count = int.from_bytes(payload[:4], 'little')
    if len(payload) == 4 + count * RECORD.size:
        return [decode_record(payload[4+i*24:4+(i+1)*24]) for i in range(count)]
    events = []
    offset = 0
    while offset < len(payload):
        remaining = len(payload) - offset
        if remaining < 4:
            raise ValueError('partial event count')
        count = int.from_bytes(payload[offset:offset+4], 'little')
        size = 4 + count * RECORD.size
        if count == 21 and remaining >= 512:
            if payload[offset+508:offset+512] != bytes(4):
                raise ValueError('nonzero full-block padding')
            events.extend(decode_record(payload[offset+4+i*24:offset+4+(i+1)*24])
                          for i in range(count))
            offset += 512
        elif count <= 21 and remaining == size:
            events.extend(decode_record(payload[offset+4+i*24:offset+4+(i+1)*24])
                          for i in range(count))
            offset += size
        else:
            raise ValueError('receive count and transfer length disagree')
    return events

def encode_observed_tx(frame: CANFrame, transaction=0x0106, channel=0):
    """Capture-derived two-channel Classical-CAN encoding.

    RTR uses flag 0x04. Unused bytes are initialized
    instead of copying the observed driver's stale DLC padding.
    transaction=0x0106 was observed; its broader semantics remain unverified.
    """
    if not isinstance(frame, CANFrame):
        raise TypeError('frame must be CANFrame')
    if not 0 <= transaction <= 65535:
        raise ValueError('transaction out of range')
    if channel not in (0, 1):
        raise ValueError("channel must be 0 or 1")
    identifier = frame.arbitration_id | (0x80000000 if frame.extended else 0)
    return RECORD.pack(1, channel, transaction, bytes(6), identifier,
                       frame.dlc, 5 if frame.remote else 1, frame.data.ljust(8,b'\0'))

# SJA1000 ECC bit layout, NXP datasheet tables 19–21. Byte offset 14 of
# wire tag 0x02 is evidenced by the ACK-error trace and CANalyzer screenshot.
ERROR_TYPES = ('bit_error', 'form_error', 'stuff_error', 'other_error')
ERROR_SEGMENTS = {
    0x03: 'start_of_frame', 0x02: 'id_28_21', 0x06: 'id_20_18',
    0x04: 'srtr', 0x05: 'ide', 0x07: 'id_17_13', 0x0f: 'id_12_5',
    0x0e: 'id_4_0', 0x0c: 'rtr', 0x0d: 'reserved_1', 0x09: 'reserved_0',
    0x0b: 'dlc', 0x0a: 'data', 0x08: 'crc_sequence', 0x18: 'crc_delimiter',
    0x19: 'ack_slot', 0x1b: 'ack_delimiter', 0x1a: 'end_of_frame',
    0x12: 'intermission', 0x11: 'active_error_flag', 0x16: 'passive_error_flag',
    0x13: 'tolerate_dominant_bits', 0x17: 'error_delimiter', 0x1c: 'overload_flag',
}

@dataclass(frozen=True)
class CANError:
    ecc: int
    direction: str
    error_type: str
    segment_code: int
    segment: str
    bus_status_raw: int
    tx_counter_candidate: int
    rx_counter_candidate: int
    controller_status_raw: int


def decode_can_error(event):
    """Only tag 2 is an error. Non-CAN unions can contain stale ECC bytes.

    Counter offsets 11/12 agree with observed error progression but have not
    been independently cross-checked against a CANalyzer counter export.
    Status bytes are retained without assigning unverified bus-off semantics.
    """
    if event.tag != 2:
        return None
    if event.channel_index not in (0, 1):
        raise ValueError('unobserved error channel')
    ecc = event.raw[14]
    segment = ecc & 0x1f
    return CANError(ecc, 'rx_error' if ecc & 0x20 else 'tx_error',
                    ERROR_TYPES[ecc >> 6], segment,
                    ERROR_SEGMENTS.get(segment, 'unknown'), event.raw[10],
                    event.raw[11], event.raw[12], event.raw[15])
