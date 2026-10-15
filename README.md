# CANcase XL Linux driver

Experimental native SocketCAN driver for the Vector CANcase XL (USB 1248:1001).
Kernel module version: **0.1.16**. Firmware: **8.7.16**.

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

This is experimental, independently developed software, not an official Vector driver.
Classical CAN only; CAN FD is not supported. Full hardware bus-off state/counter interpretation and recovery remain unverified.
The default CAN-error isolation stops the affected channel; it must not be interpreted as verified hardware bus-off reporting.
Extended-ID hardware acceptance filters remain unfinished. Hardware timestamps use raw device time; see [TIMESTAMPS.md](TIMESTAMPS.md).

Some older devices change their USB descriptor from one configuration to two after firmware loading while Linux retains the original descriptor.
The loader detects this mismatch and stops: runtime configuration 2 cannot be selected.
There is currently no supported workaround for that device transition in this driver.

Devices with low serial numbers, e.g. 01xxxx, do not work in my tests and exhibit the behavior described above. 
Devices with serial numbers starting with 05xxxx, on the other hand, all work.

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
