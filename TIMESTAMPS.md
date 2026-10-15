# Hardware timestamps

Driver 0.1.16 passes raw hardware timestamps to SocketCAN for received Classical CAN data/RTR frames and successful transmit receipts. TX echo is stamped before the CAN echo helper delivers it; requested TX timestamps also reach the sending socket's error queue. Synthesized error/status frames are not hardware timestamped.

## Usage

Applications must request hardware timestamping. With can-utils:

```sh
candump -H can123456_1 can123456_2
```

Socket applications can request SOF_TIMESTAMPING_RX_HARDWARE, SOF_TIMESTAMPING_TX_HARDWARE and SOF_TIMESTAMPING_RAW_HARDWARE through SO_TIMESTAMPING. The third timespec in SCM_TIMESTAMPING contains the raw hardware timestamp. Hardware configuration is fixed ON/ALL. No PTP hardware clock is exposed.

## Clock semantics

- Event bytes 4..9 contain a 48-bit little-endian counter at 16 MHz.
- Each tick is 62.5 ns; integer nanoseconds truncate half a nanosecond. Resolution is not an accuracy specification.
- Both channels of one device share the observed clock domain.
- Values are raw device time, not host wall time or UTC. Separate devices are not synchronized.
- No host anchoring, drift correction or monotonic clamping is applied.
- Firmware restart can reset the counter. The raw counter wraps after approximately 203.6 days; these discontinuities remain visible.
- Channel down/up does not reset any driver timestamp state. Continuity across a channel restart has not been established by the current tests.
- An exact zero value is indistinguishable from the SocketCAN absent-timestamp sentinel.

## Validation scope

The module was built and installed on Linux 7.0.0-38-generic. Hardware tests verified raw RX, TX echo and TX error-queue delivery on two CANcase XL devices, at 500 kbit/s and 1 Mbit/s across the test set. A second device also passed after an individual channel restart and USB unplug/replug.

The tests verify timestamp delivery and increasing values within each run. They do not establish absolute timing accuracy, cross-device synchronization, continuity between test runs, or long-term counter-wrap behavior.

Python hardware tests and synthetic conversion tests are available only on the [testing branch](https://github.com/Snolte1001/cancasexl-linux/tree/testing). No recorded logs or captures are included.

Reference: https://docs.kernel.org/networking/timestamping.html
