# Experimental hardware timestamps (0.1.16)

CAN data and RTR receive events and successful transmit receipts carry a
48-bit little-endian counter at record offsets 4..9. Matching USB and CANalyzer
events supports a 16 MHz clock: one tick is 62.5 ns. Resolution is not an
accuracy specification. The driver converts with `(ticks * 125) >> 1`, truncating
half a nanosecond, and fills `skb_hwtstamps()` before delivery.

RX frames and local TX echo carry raw hardware timestamps. Requested TX
hardware timestamps are also delivered through the socket error queue by the
CAN echo helper. Synthesized error/status frames are not hardware timestamped.
The ethtool capability report advertises hardware RX/TX and no PTP clock.
Timestamping is fixed ON/ALL; disabling it through SIOCSHWTSTAMP is unsupported.

## Clock semantics

- These are raw device times, not UTC or host receive times.
- Both channels of one device share the observed clock domain.
- Separate devices are not synchronized. Do not compare their absolute values.
- No host-time anchoring, smoothing, drift correction or per-channel offset is applied.
- Firmware restart may reset the counter. The driver does not hide the jump.
- The raw 48-bit counter wraps after approximately 203.6 days. This initial
  implementation deliberately exposes the wrap rather than guessing reset vs. wrap.
- Channel down/up does not reset any driver timestamp state; no such state is kept.
  Firmware behavior across channel restarts still requires hardware validation.
- Zero is SocketCAN's absent-timestamp sentinel; an event at exact device tick zero
  cannot be distinguished from an absent stamp.

## Install and test

Disconnect the CANcase before installing the testing branch:

```sh
sudo bash kernel/install-autostart.sh
```

Reconnect it and verify `cat /sys/module/cancasexl/version` reports `0.1.16`.
Connect both channels of the SAME device to one correctly terminated test bus.
For serial 050996:

```sh
sudo ip link set can050996_1 down
sudo ip link set can050996_2 down
sudo ip link set can050996_1 type can bitrate 500000
sudo ip link set can050996_2 type can bitrate 500000
sudo ip link set can050996_1 up
sudo ip link set can050996_2 up
python3 tools/timestamp_test.py --interfaces can050996_1 can050996_2
```

The test transmits standard data frames with IDs 0x650 and 0x651 in both directions.
It requires nonzero raw hardware timestamps on received frames, local echo and
TX error-queue messages. Software timestamps cannot satisfy it. It compares TX
echo/error-queue equality and checks increasing time separately for each path.
The RX/TX offset is printed, not treated as a precise bus-latency measurement.
No report file is created.

Repeat at 1 Mbit/s, and after down/up of one channel while the other remains UP.
Check both channels again after unplug/replug. Do not expect time continuity
across firmware reloads. `candump -H` can display hardware timestamps, but this
script is the explicit check that software fallback has not masked a failure.

Local validation covers conversion boundary cases and ancillary-data parsing.
Actual hardware delivery and the target-kernel build must still be verified.

Reference: https://docs.kernel.org/networking/timestamping.html
