# CANcase XL Linux driver

Experimental native SocketCAN driver for the Vector CANcase XL (USB 1248:1001).
Kernel module version: **0.1.21**. Firmware: **8.7.16**.

## Branches

- `main`: kernel driver, C firmware loader, installation support and documentation.
- `testing`: the same driver plus Python hardware tests and development support.


## For more details, see the Main Branch! 

## CAN error policies

Version 0.1.19 adds continuing CAN error handling and a selectable restrictive fallback. See [ERROR_MODES.md](ERROR_MODES.md). Hardware validation is pending; main remains at 0.1.16.


Bus-off reporting and SocketCAN restart support are implemented in 0.1.19. Hardware restart validation is pending; see [ERROR_MODES.md](ERROR_MODES.md).

## Module option conflicts

The installer checks `modprobe --showconfig` after installing its managed
configuration and before loading the module. It rejects conflicting repeated
options or overrides of `isolate_can_errors=0`, `diagnose_channel=0`, and
`managed_handoff=1`. Equivalent boolean values such as 0/N and 1/Y are accepted.
It lists configuration source lines so additional files can be corrected manually.
External configuration files are never automatically deleted or rewritten.

After loading, the installer also checks the three actual sysfs parameter values
and unloads the module before enabling USB triggers. A mismatch stops installation
without printing success; inspect additional modprobe files, install rules and
kernel command-line options. Earlier installation steps may already have completed;
resolve the conflict and rerun the installer before reconnecting devices.

The driver remains version 0.1.19; this change affects installation only.

## Transport fault diagnostics (0.1.20)

Fatal transport/protocol faults now log their origin before the existing
device-wide stop. Messages distinguish USB RX/TX status and actual length,
an empty successful RX transfer, invalid RX block layout, invalid CAN record
fields, and mismatched TX receipts. Receipt diagnostics compare identifiers,
lengths and payload equality without dumping payload bytes. Partial RX timeouts
retain their original USB status in parser-failure messages.

This is a diagnostic change, not a transport recovery fix. Failure policy,
normal polling, CAN error handling and bus-off restart behavior are unchanged.
No additional module option is needed. Normal traffic does not produce these
new messages.

After installing, reconnect the device during a maintenance window and verify:
```sh
cat /sys/module/cancasexl/version
```
The loaded version must match the installed package version. Reapply the required channel bitrates and
bring the interfaces up before resuming traffic.

After a failure, save the surrounding kernel messages locally:
```sh
sudo journalctl -k -b --since '10 minutes ago' --no-pager
```
Include the diagnostic lines preceding `transport/protocol fault`.
Do not commit hardware logs to this repository.

Synthetic RX transport/parser checks:
```sh
python3 tests/test_rx_diagnostics.py
```
These compile the actual RX functions with a USB stub using `cc`. They do not
replace building the complete module against the target kernel or hardware tests.

## Classical CAN raw DLC (0.1.21)

The USB record DLC field accepts values 0..15. DLC 9..15 represents eight
payload bytes, not a malformed record. RX copies, byte statistics and TX
receipt comparisons use bounded data lengths. Values above 15 remain errors.

Both channels support the standard optional `cc-len8-dlc` control mode.
With the mode off (default), DLC 9..15 is received as length 8 and TX uses
the normal length. With the mode on, SocketCAN preserves DLC 9..15 in
`can_frame.len8_dlc` and uses it for transmission when `len == 8`.
RTR frames carry no payload; their requested length is capped at 8 and the
raw DLC follows the same control-mode rules. TX receipts must match the
actual transmitted DLC, including when two DLC values map to length 8.

Example (substitute your serial number):
```sh
sudo ip link set can051969_1 down
sudo ip link set can051969_1 type can bitrate 500000 cc-len8-dlc on
sudo ip link set can051969_1 up
candump -8 can051969_1
```

Use an external CAN sender on a terminated test bus. Send Classical CAN ID
0x621 with eight bytes, first with DLC 8 and then DLC 15. Verify both arrive,
the raw DLC is visible, and the channel remains ERROR-ACTIVE. Repeat on the
other channel. Test DLC 0..15, standard/extended IDs, RTR and Linux TX before
promoting this change to main.

Windows CAN/USB comparison confirmed raw DLC 8 and 15 in TX, RX and TX
receipt records. Linux hardware validation remains pending. No capture files
are stored in the repository.

Run `python3 tests/test_classic_dlc.py` for 256 synthetic RX/echo combinations
covering both channels, control mode on/off, standard/extended IDs, data/RTR
and DLC 0..15, plus rejection of all byte values above 15.
