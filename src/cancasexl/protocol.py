"""Commands evidenced by four captures and vcasexl.sys 8.7.16.0.
The final byte of bitmap/download commands is unused Windows buffer padding.
"""
import hashlib
import struct
from pathlib import Path

SYS_SHA256 = '492e96dbee1578cf3344cf6c62956f63354430eea6b30063ec53ce1b0362b0bc'
FW_SHA256 = '52f448ab740bd1f328cfcd4cafbc142315b938a0ab3ccd968b06f8660a5aba9b'
FW_OFFSET, FW_LENGTH, RAM_BASE = 293728, 145738, 0x15000
# Tuple order: TSEG1, TSEG2, SJW, SAM. VHwConfReport.log confirms the
# 500 kbit/s profile against the captured opcode 0x1D request.
PROFILES = {1000000: (5, 2, 2, 1), 500000: (4, 3, 1, 1),
            250000: (5, 2, 2, 1), 125000: (5, 2, 2, 1)}

def extract_firmware(path):
    data = Path(path).read_bytes()
    if hashlib.sha256(data).hexdigest() != SYS_SHA256:
        raise ValueError('requires the supplied vcasexl.sys 8.7.16.0; driver hash differs')
    firmware = data[FW_OFFSET:FW_OFFSET + FW_LENGTH]
    validate_firmware(firmware)
    return firmware

def validate_firmware(firmware):
    if len(firmware) != FW_LENGTH or hashlib.sha256(firmware).hexdigest() != FW_SHA256:
        raise ValueError('unverified firmware image; refusing download')

def download_packets(firmware):
    validate_firmware(firmware)
    for offset in range(0, len(firmware), 1265):
        data = firmware[offset:offset + 1265]
        final = int(offset + len(data) == len(firmware))
        yield struct.pack('<HHHII', 0x40, final, 0, RAM_BASE + offset, len(data)) + data + b'\0'

def channel_command(opcode, channel):
    if channel not in (0, 1):
        raise ValueError('channel must be 0 or 1')
    return struct.pack('<HH', opcode, channel)

def bitrate_command(channel, bitrate):
    if bitrate not in PROFILES:
        raise ValueError('only captured 125000, 250000, 500000 and 1000000 bit/s profiles supported')
    return channel_command(0x1d, channel) + struct.pack('<IIIII', bitrate, *PROFILES[bitrate])

def filter_command(channel, enabled):
    return channel_command(0x22, channel) + bytes.fromhex('010000000001') + bytes([255 if enabled else 0]) * 256 + b'\0'

def check_reply(request, response, length=6):
    if response is None:
        raise TimeoutError(f'USB command {int.from_bytes(request[:2], "little"):#x} reply timed out; command is not retried')
    opcode = int.from_bytes(request[:2], 'little') | 0x80
    if len(response) != length or int.from_bytes(response[:2], 'little') != opcode:
        raise IOError(f'unexpected reply to opcode {opcode & ~0x80:#x}: {response.hex()}')
    status = int.from_bytes(response[2:4], 'little')
    if status:
        raise IOError(f'device command failed: status {status:#x}')
    return response
