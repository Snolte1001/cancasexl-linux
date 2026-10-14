"""Experimental Linux userspace driver; selected loopback tests hardware-validated.
Single-threaded owner of both channels. No SocketCAN or automatic kernel detach.
"""
import argparse
import json
import struct
import sys
import time
from collections import deque
from .driver import CANFrame
from .protocol import (extract_firmware, download_packets, check_reply,
                       channel_command, bitrate_command, filter_command)
from .wire import encode_observed_tx, decode_receive_transfer, decode_can_error

class NativeUSB:
    def __init__(self, bus, address):
        import usb.core
        import usb.util
        self.core, self.util = usb.core, usb.util
        self.device = usb.core.find(idVendor=0x1248, idProduct=0x1001,
                                    bus=bus, address=address)
        self.claimed = False
        if self.device is None:
            raise RuntimeError('selected 1248:1001 device not found')
        self.endpoints = set()
        self.timeout = 2000

    def configuration(self):
        try:
            return self.device.get_active_configuration().bConfigurationValue
        except self.core.USBError:
            # Standard GET_CONFIGURATION distinguishes unconfigured from I/O failure.
            return int(self.device.ctrl_transfer(0x80, 8, 0, 0, 1, timeout=self.timeout)[0])

    def select_configuration(self, value):
        if self.claimed:
            self.util.release_interface(self.device, 0)
            self.claimed = False
        self.endpoints.clear()
        current = self.configuration()
        if current != 0 and self.device.is_kernel_driver_active(0):
            raise RuntimeError('kernel driver owns interface; automatic detach disabled')
        if current != value:
            self.device.set_configuration(value)
        if value == 0:
            return
        cfg = self.device.get_active_configuration()
        intf = cfg[(0, 0)]
        endpoints = {e.bEndpointAddress for e in intf if e.bmAttributes & 3 == 2}
        expected = {1, 0x81} if value == 1 else {1, 0x81, 0x82, 3, 4}
        if endpoints != expected or any(e.wMaxPacketSize != 512 for e in intf):
            raise RuntimeError('USB descriptors differ from captured device')
        self.util.claim_interface(self.device, 0)
        self.claimed = True
        self.endpoints = endpoints

    def write(self, endpoint, payload):
        if endpoint not in self.endpoints or endpoint & 0x80:
            raise ValueError('invalid OUT endpoint')
        size = self.device.write(endpoint, payload, timeout=self.timeout)
        if size != len(payload):
            raise IOError('short USB write; no automatic retry')

    def read(self, endpoint, length=65536, timeout_ms=100):
        if endpoint not in self.endpoints or not endpoint & 0x80:
            raise ValueError('invalid IN endpoint')
        try:
            return bytes(self.device.read(endpoint, length, timeout=timeout_ms))
        except self.core.USBTimeoutError:
            return None

    def start_firmware(self):
        self.select_configuration(0)
        self.device.ctrl_transfer(0x40, 1, 1, 0, b'', timeout=self.timeout)
        # Same device/address in captures; fail explicitly if Linux re-enumerates it.
        time.sleep(0.1)
        self.select_configuration(2)

    def close(self):
        try:
            if self.claimed:
                self.util.release_interface(self.device, 0)
        finally:
            self.claimed = False
            self.endpoints.clear()
            self.util.dispose_resources(self.device)

class ExperimentalDriver:
    def __init__(self, io):
        self.io = io
        self.started = set()
        self.pending = deque()
        self.ready = False
        self.closed = False
        self.faulted = False
        self.bitrates = {}

    def command(self, payload, length=6):
        if self.faulted:
            raise RuntimeError('command stream desynchronized; unplug/replug required')
        try:
            self.io.write(1, payload)
            return check_reply(payload, self.io.read(0x81, 512, 2000), length)
        except Exception:
            self.faulted = True
            raise

    def open(self, bitrate=500000, driver_path=None, channel_bitrates=None,
             start_channels=(0, 1)):
        if self.ready or self.closed:
            raise RuntimeError('session cannot be reopened')
        # Validate all parameters before USB mutation.
        rates = (bitrate, bitrate) if channel_bitrates is None else tuple(channel_bitrates)
        if len(rates) != 2:
            raise ValueError('exactly two channel bitrates required')
        for channel, rate in enumerate(rates):
            bitrate_command(channel, rate)
        selected = tuple(start_channels)
        if len(set(selected)) != len(selected) or any(c not in (0, 1) for c in selected):
            raise ValueError('start_channels must contain distinct channel indices 0/1')
        config = self.io.configuration()
        if config in (0, 1):
            if driver_path is None:
                raise ValueError('cold device needs --driver /path/vcasexl.sys')
            firmware = extract_firmware(driver_path)
            self.io.select_configuration(1)
            boot = self.command(b'\x41\0' + bytes(58), 60)
            # Offset 12 is unknown: Windows captures report 9, first Linux test 0.
            # Accept the two captured bootcode variants; keep the unknown-field gate.
            identity = (boot[:12], struct.unpack_from('<I', boot, 12)[0])
            observed = {
                (bytes.fromhex('c10000000c000003a7000000'), 0),
                (bytes.fromhex('c10000000c000003a7000000'), 9),
                (bytes.fromhex('c10000000a000601a7000000'), 9),
            }
            if identity not in observed:
                raise RuntimeError('bootloader identity differs from observed devices: ' + boot.hex(' '))
            for packet in download_packets(firmware):
                self.command(packet)
            self.io.start_firmware()
        elif config == 2:
            self.io.select_configuration(2)
        else:
            raise RuntimeError('unobserved USB configuration')
        self.command(b'\x33\0')
        self.command(bytes.fromhex('2a00000000000000'))
        version = self.command(b'\x24\0' + bytes(22), 24)
        if struct.unpack_from('<III', version, 4) != (8, 7, 16):
            raise RuntimeError('runtime firmware version differs from captured 8.7.16')
        if config in (0, 1):
            # All six cold Windows traces query each piggy/channel with 0x04
            # and receive asynchronous tag-5 channel reports before proceeding.
            for channel in (0, 1):
                self.command(channel_command(0x04, channel) + bytes(2))
                deadline = time.monotonic() + 2.0
                reported = False
                while time.monotonic() < deadline:
                    data = self.io.read(0x82, 65536, 100)
                    if data is None:
                        continue
                    events = decode_receive_transfer(data)
                    self.pending.extend(events)
                    if any(event.tag == 5 and event.channel_index == channel for event in events):
                        reported = True
                        break
                if not reported:
                    raise TimeoutError(f'channel {channel + 1} initialization report missing after opcode 0x04')
        # Global request seen after cold queries and before start in warm traces.
        # Its full semantics are still unknown; no EEPROM writes are reproduced.
        self.command(bytes.fromhex('05000100'))
        self.command(bytes.fromhex("3100000000000000"))
        self.command(bytes.fromhex("2a000000fa000000"))
        # Only reused runtime sessions need a pre-stop. Windows cold-start traces
        # do not stop before starting; Linux cold firmware rejects that request.
        for channel in (0, 1):
            if config == 2:
                self.command(channel_command(0x19, channel))
            self.command(channel_command(0x23, channel) + bytes(8))
            self.command(filter_command(channel, channel in selected))
            self.bitrates[channel] = rates[channel]
            if channel not in selected:
                continue
            self.command(bitrate_command(channel, rates[channel]))
            self.command(channel_command(0x1c, channel) + struct.pack('<I', 1))
            # Mark attempted start before write: timeout may still mean hardware started.
            self.started.add(channel)
            self.command(channel_command(0x18, channel))
            self.bitrates[channel] = rates[channel]
        self.ready = True

    def stop_channel(self, channel):
        self._check_channel(channel)
        if channel not in self.started:
            raise RuntimeError('channel is already stopped')
        self.command(channel_command(0x20, channel) + struct.pack('<I', 9))
        self.command(channel_command(0x19, channel))
        self.command(filter_command(channel, False))
        self.started.remove(channel)

    def start_channel(self, channel, bitrate):
        self._check_channel(channel)
        bitrate_command(channel, bitrate)  # Reject before any USB mutation.
        if channel in self.started:
            raise RuntimeError('stop the channel before changing its bitrate')
        self.command(channel_command(0x23, channel) + bytes(8))
        self.command(filter_command(channel, True))
        self.command(bitrate_command(channel, bitrate))
        self.command(channel_command(0x1c, channel) + struct.pack('<I', 1))
        self.started.add(channel)
        self.command(channel_command(0x18, channel))
        self.bitrates[channel] = bitrate

    def _check_channel(self, channel):
        if channel not in (0, 1):
            raise ValueError('channel must be 0 or 1')
        if not self.ready or self.closed or self.faulted:
            raise RuntimeError('driver is not ready')

    def send(self, channel, frame):
        self._check_channel(channel)
        if channel not in self.started:
            raise RuntimeError('cannot send on stopped channel')
        self.io.write(3 + channel, encode_observed_tx(frame, channel=channel))
        # USB acceptance is not a CAN send confirmation. Read tx_receipt separately.

    def receive(self, timeout_ms=100):
        if not self.ready or self.closed or self.faulted:
            raise RuntimeError('driver is not ready')
        if not self.pending:
            data = self.io.read(0x82, 65536, timeout_ms)
            if data is None:
                return None
            self.pending.extend(decode_receive_transfer(data))
        return self.pending.popleft() if self.pending else None

    def close(self):
        if self.closed:
            return
        errors = []
        try:
            for channel in sorted(self.started):
                if self.faulted:
                    errors.append('command stream uncertain; unplug/replug to stop device')
                    break
                try:
                    self.command(channel_command(0x20, channel) + struct.pack('<I', 9))
                    self.command(channel_command(0x19, channel))
                    self.command(filter_command(channel, False))
                except Exception as exc:
                    errors.append(str(exc))
        finally:
            self.closed = True
            self.ready = False
            self.started.clear()
            self.io.close()
        if errors:
            raise RuntimeError('cleanup incomplete: ' + '; '.join(errors))

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        try:
            self.close()
        except Exception as exc:
            if kind is None:
                raise
            print(str(exc), file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bus', type=int, required=True)
    parser.add_argument('--address', type=int, required=True)
    parser.add_argument('--driver', help='original Windows vcasexl.sys (8.7.16.0)')
    parser.add_argument('--bitrate', type=int, choices=(125000, 250000, 500000, 1000000), default=500000)
    parser.add_argument('--seconds', type=float, default=10)
    parser.add_argument('--tx-channel', type=int, choices=(1, 2), help='send one 0x123 test frame')
    parser.add_argument('--log', help='JSONL output; diagnostic events included')
    args = parser.parse_args()
    if args.seconds <= 0:
        parser.error('--seconds must be positive')
    output = open(args.log, 'w', encoding='utf-8') if args.log else sys.stdout
    try:
        with ExperimentalDriver(NativeUSB(args.bus, args.address)) as driver:
            driver.open(args.bitrate, args.driver)
            print('Both channels started in normal mode; waiting for events.', file=sys.stderr)
            if args.tx_channel:
                driver.send(args.tx_channel - 1, CANFrame(0x123, bytes.fromhex('1122334455667788')))
            overrun_channels = set()
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline:
                event = driver.receive()
                if event is None:
                    continue
                if event.hardware_overrun and event.channel_index not in overrun_channels:
                    overrun_channels.add(event.channel_index)
                    print(f'Hardware queue overrun on CAN {event.channel_index + 1}; data was lost.',
                          file=sys.stderr)
                row = dict(tag=event.tag, channel=event.channel_index + 1, channel_raw=event.channel,
                           hardware_overrun=event.hardware_overrun,
                           transaction=event.transaction, timestamp_ticks=event.timestamp_ticks,
                           flags=event.flags_raw, raw=event.raw.hex())
                error = decode_can_error(event)
                if error is not None:
                    row.update(direction=error.direction, ecc=hex(error.ecc),
                               error_type=error.error_type, segment=error.segment,
                               segment_code=error.segment_code, bus_status_raw=error.bus_status_raw,
                               tx_counter_candidate=error.tx_counter_candidate,
                               rx_counter_candidate=error.rx_counter_candidate,
                               controller_status_raw=error.controller_status_raw)
                try:
                    frame = event.can_frame
                    if frame:
                        row.update(direction=event.direction, id=hex(frame.arbitration_id),
                                   extended=frame.extended, dlc=frame.dlc, data=frame.data.hex())
                except ValueError as exc:
                    row['decode_error'] = str(exc)
                print(json.dumps(row), file=output, flush=True)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f'CANcase XL: {exc}', file=sys.stderr)
        return 1
    finally:
        if output is not sys.stdout:
            output.close()
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
