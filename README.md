# CANcase XL Linux driver

Experimental native SocketCAN driver for the Vector CANcase XL (USB 1248:1001).
Kernel module version: **0.1.21**. Firmware: **8.7.16**.

This device has been marked as end-of-life by the manufacturer and is no longer supported in newer versions of CANalyzer, etc. 
However, there are still many of these devices in use that can be given a new lease on life with SocketCAN on Linux using this driver. (Windows drivers for Python via XL-API are also available.) 

## Branches

- `main`: kernel driver, C firmware loader, installation support and documentation.
- `testing`: the same driver plus Python hardware tests and development support.

Python is not required to install or run the kernel driver, just to run the test scripts in testing Branch.

## Features

- Two independently controlled Classical CAN channels.
- Standard and extended identifiers, data frames and RTR frames (DLC 0–8).
- 125, 250, 500 kbit/s and 1 Mbit/s.
- Concurrent transmission and multiple connected CANcase XL devices.
- Stable interface names based on the serial number: `can123456_1`, `can123456_2`.
- DKMS installation, USB hotplug firmware preparation and suspend/resume helpers.
- Read-only device information, piggyback identification and cached license metadata.

## Installation (Ubuntu/Debian)

Disconnect all CANcase XL devices before installing or updating. Other CAN adapters may remain connected.

```sh
sudo apt install build-essential dkms libssl-dev linux-headers-$(uname -r) iproute2 can-utils
sudo bash kernel/install-autostart.sh
```

Connect the device after installation. Firmware is loaded into RAM automatically.
Both channels initially remain DOWN. Find their names with:

```sh
cancasexl-info
ip -details link show type can
```

Replace the example serial number below with your device serial number:

```sh
sudo ip link set can123456_1 type can bitrate 500000
sudo ip link set can123456_2 type can bitrate 500000
sudo ip link set can123456_1 up
sudo ip link set can123456_2 up
```
Usage in candump:
```sh
candump can123456_1 can123456_2
```

Use a correctly terminated CAN bus and another active node for acknowledgements.
Bring a channel DOWN before changing its bitrate. Separate physical buses may use different bitrates.

```sh
cansend can123456_1 321#1122334455667788
cansend can123456_1 321#R8
cansend can123456_1 001ABCDE#R4
```

## Hardware timestamps

Version 0.1.16 supplies raw hardware timestamps for received frames and successful transmit receipts, including local TX echo and requested TX error-queue timestamps.

```sh
candump -H can123456_1 can123456_2
```

These are device-clock values, not UTC timestamps. Different CANcase devices are not synchronized. See [TIMESTAMPS.md](TIMESTAMPS.md) for clock semantics and validation scope.

## Bus-off recovery

The default `restart-ms 0` disables automatic bus-off recovery. After removing
the fault, request manual recovery with `ip link set INTERFACE type can restart`
or cycle down/up. Set a positive `restart-ms` while the interface is down to
enable a delayed restart. Pending transmissions are discarded on bus-off.
See [ERROR_MODES.md](ERROR_MODES.md) for configuration and limitations.

## Device information

```sh
cancasexl-info --serial 123456 --json
cancasexl-info --serial 123456 --licenses --json
cancasexl-info --serial 123456 --export "$HOME/CANcase_123456"
```

The export command creates a `.vlurep` report and a plain-text `.json` file outside the repository.
These describe cached probe-time data. They do not install or modify licenses.
The initial transceiver status is not a live status measurement.

## Diagnostics

```sh
cat /sys/module/cancasexl/version
dkms status -m cancasexl
sudo journalctl -b -u 'cancasexl-prepare@*' --no-pager -n 60
sudo dmesg --level=err,warn
```
## Removal

```sh
sudo bash kernel/uninstall-autostart.sh
```

Uninstalling does not forcibly unload an active module. Stop your CAN applications before unloading it or rebooting.
DKMS needs matching kernel headers after kernel updates. Secure Boot may require enrolling the DKMS signing key.

## Limitations

This is experimental, independently developed software, not an official Vector driver ! 

Classical CAN only; CAN FD is not supported by Hardware. Bus-off reporting, manual restart and timed restart are supported; hardware validation scope is documented in [ERROR_MODES.md](ERROR_MODES.md). Bus-off counter bytes are not exposed as valid error counters.
By default, verified CAN errors keep the controller running. Set `isolate_can_errors=1` to stop the affected channel on its first reported CAN error. See [ERROR_MODES.md](ERROR_MODES.md) for recovery behavior, switching modes and validation scope.
Extended-ID hardware acceptance filters remain unfinished. Hardware timestamps use raw device time; see [TIMESTAMPS.md](TIMESTAMPS.md).

Some older devices change their USB descriptor from one configuration to two after firmware loading while Linux retains the original descriptor.
The loader detects this mismatch and stops: runtime configuration 2 cannot be selected.
There is currently no supported workaround for that device transition in this driver.

Devices with low serial numbers, e.g. 01xxxx, (hardware version 2) do not work in my tests and exhibit the behavior described above. 
Devices with serial numbers 02xxxx and higher (all with hardware version 3), on the other hand, work without any problems. 

Multiple-device hotplug and RTR traffic have been exercised on physical hardware, but this does not establish compatibility with every hardware revision or host controller.

## Building the module only

```sh
make -C kernel
```

Manual module loading requires prepared runtime firmware. The autostart installer handles preparation and driver binding.
A full kernel build is unnecessary; matching kernel headers are sufficient.

## Firmware and licensing

Source files retain their SPDX license identifiers. The bundled RAM firmware is a separate Vector binary and is not covered by the source-code license.
Alternatively, supply the supported original driver file to the installer:

```sh
sudo bash kernel/install-autostart.sh /path/to/vcasexl.sys
```

The loader verifies the input hash. Arbitrary firmware versions are not accepted.

The vcasexl.sys file can be extracted from the Vector Driver Setup, which is available for download on the Vector website. 
Newer versions of the driver package may no longer include this file, as the XL series has reached end of life.

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

The module-option checks affect installation; the runtime error policy is configured separately.

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
sudo journalctl -k -b --no-pager
```
Include the diagnostic lines preceding `transport/protocol fault`.
Do not commit hardware logs to this repository.


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

Hardware validation confirmed DLC 8 and 15 reception and transmission on both channels at 500 kbit/s, with external CANalyzer comparison. With `cc-len8-dlc off`, DLC 15 is received as length 8. Additional testing was reported successful before promotion to main. This does not establish hardware coverage of every DLC, identifier type, RTR combination or bitrate.

Synthetic regression tests remain on the `testing` branch. No hardware logs or captures are stored in this repository.
