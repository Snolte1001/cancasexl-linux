"""Device lifecycle separated from the currently unknown Vector USB codec.

Never equate XLevent memory layout with a USB packet format.
One instance owns one logical channel; a future codec must coordinate shared
hardware if multiple logical channels are needed. No SocketCAN registration yet.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass

class ProtocolUnavailable(RuntimeError):
    pass

@dataclass(frozen=True)
class CANFrame:
    arbitration_id: int
    data: bytes = b""
    extended: bool = False
    remote: bool = False
    dlc: int | None = None

    def __post_init__(self):
        if not isinstance(self.data, bytes):
            raise TypeError("data must be bytes")
        maximum = 0x1FFFFFFF if self.extended else 0x7FF
        if not 0 <= self.arbitration_id <= maximum:
            raise ValueError("CAN identifier out of range")
        dlc = len(self.data) if self.dlc is None else self.dlc
        if not 0 <= dlc <= 8:
            raise ValueError("Classical CAN requires DLC 0..8")
        if self.remote and self.data:
            raise ValueError("RTR frames have no data")
        if not self.remote and len(self.data) != dlc:
            raise ValueError("DLC must match data length")
        object.__setattr__(self, "dlc", dlc)

class Codec(ABC):
    """Implement only after USB commands and responses have been validated.

    configure/start/stop must exchange and check device responses, not just
    write bytes. receive must handle buffering, partial messages, ACK/error
    records and channel routing. send must distinguish USB acceptance from
    successful CAN transmission. Transport supports control/bulk/interrupt.
    """
    @abstractmethod
    def configure(self, transport, channel: int, bitrate: int): ...
    @abstractmethod
    def start(self, transport): ...
    @abstractmethod
    def send(self, transport, frame: CANFrame): ...
    @abstractmethod
    def receive(self, transport, timeout_ms: int) -> CANFrame | None: ...
    @abstractmethod
    def stop(self, transport): ...

class CANcaseXL:
    def __init__(self, transport, codec: Codec | None = None):
        self.transport, self.codec = transport, codec
        self.active = False

    def open(self, channel=0, bitrate=500000):
        if self.active:
            raise RuntimeError("already active")
        if self.codec is None:
            raise ProtocolUnavailable(
                "CANcase XL USB codec is unknown. Capture Windows USB traffic; "
                "the XL API manual does not specify device packets.")
        if channel not in (0, 1) or not 15000 <= bitrate <= 1000000:
            raise ValueError("channel must be 0/1, bitrate 15000..1000000")
        self.transport.open()
        try:
            self.codec.configure(self.transport, channel, bitrate)
            self.codec.start(self.transport)
        except BaseException:
            # No guessed recovery command after partial initialization.
            self.transport.close()
            raise
        self.active = True
        return self

    def send(self, frame):
        if not self.active:
            raise RuntimeError("device is not active")
        if not isinstance(frame, CANFrame):
            raise TypeError("frame must be CANFrame")
        return self.codec.send(self.transport, frame)

    def receive(self, timeout_ms=100):
        if not self.active:
            raise RuntimeError("device is not active")
        if timeout_ms <= 0:
            raise ValueError("timeout must be positive")
        return self.codec.receive(self.transport, timeout_ms)

    def close(self):
        if self.active:
            self.active = False
            try:
                self.codec.stop(self.transport)
            finally:
                self.transport.close()
