# CANcase XL Linux driver

Experimental native SocketCAN driver for the Vector CANcase XL (USB 1248:1001).
Kernel module version: **0.1.19**. Firmware: **8.7.16**.

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
