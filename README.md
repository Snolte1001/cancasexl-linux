# CANcase XL Linux driver

Experimental native SocketCAN driver for the Vector CANcase XL (USB 1248:1001).
Kernel module version: **0.1.18**. Firmware: **8.7.16**.

## Branches

- `main`: kernel driver, C firmware loader, installation support and documentation.
- `testing`: the same driver plus Python hardware tests and development support.


## For more details, see the Main Branch! 

## CAN error policies

Version 0.1.18 adds continuing CAN error handling and a selectable restrictive fallback. See [ERROR_MODES.md](ERROR_MODES.md). Hardware validation is pending; main remains at 0.1.16.
