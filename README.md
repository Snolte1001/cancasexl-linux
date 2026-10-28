# CANcase XL Linux driver

Experimental native SocketCAN driver for the Vector CANcase XL (USB 1248:1001).
Kernel module version: **0.1.20**. Firmware: **8.7.16**.

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
The loaded version must be `0.1.20`. Reapply the required channel bitrates and
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
