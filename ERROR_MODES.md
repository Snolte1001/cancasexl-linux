# CAN error policies (0.1.18)

The default `isolate_can_errors=0` now reports verified CAN errors and keeps the
controller running. `isolate_can_errors=1` restores the restrictive policy: stop
the affected channel on its first reported CAN error, then require down/up.
The parameter is read-only while loaded and applies to every attached CANcase.
Keep `diagnose_channel=0` for normal use.

## Pending transmission and recovery

After a verified TX-side CAN error, continuing mode retains the outstanding
frame and echo slot. The ordinary five-second receipt timeout is suppressed for
that frame. The controller may retransmit; the driver does not submit a duplicate
or report an unsuccessful attempt as a successful transmission. A later hardware
receipt releases the slot. Until then, further sends experience backpressure.
This can last indefinitely; administrative down/up aborts the pending send.
RX-side errors alone do not suppress the TX receipt watchdog.

A missing TX receipt without a TX-side CAN error stops only that channel after
five seconds. Unknown controller/error status also stops only the affected
channel. Failure of a USB stop command still escalates to a device-wide transport
fault because the command stream is then uncertain.

Only Active, Warning and Passive firmware states are verified. True bus-off
encoding and recovery remain unverified. This release does not implement automatic
bus-off restart or claim full candleLight-equivalent recovery.

## Select restrictive mode

Edit the existing managed option:

```sh
sudo sed -i 's/isolate_can_errors=0/isolate_can_errors=1/' /etc/modprobe.d/cancasexl-autostart.conf
```

Stop applications and unplug all CANcase devices, then unload the module:

```sh
sudo rmmod cancasexl
```

Reconnect the device and check:

```sh
cat /sys/module/cancasexl/version
cat /sys/module/cancasexl/parameters/isolate_can_errors
```

`Y` means restrictive; `N` means continuing. To restore continuing mode, replace
`isolate_can_errors=1` with `isolate_can_errors=0` and repeat unload/reconnect.
Reinstalling restores the installer default (continuing mode).

## Hardware validation

Version 0.1.18 was exercised on physical hardware at 500 kbit/s with two
connected, terminated channels and no additional acknowledgement node.

- Continuing mode: ACK loss reached Error-Passive; restarting the peer succeeded
  under the error stream. The pending frame and subsequent traffic arrived.
- Both channel directions recovered to Error-Active without restarting the
  transmitting channel. No transport timeout or dropped frame was observed.
- Restrictive mode: the first reported CAN error stopped the affected channel
  and discarded its pending frame. Down/up restored successful communication.
- Reported error counters are cached from firmware events, not continuously
  sampled. Historical error statistics remain after recovery.

These checks validate ACK-loss recovery, not true bus-off or every CAN fault.
True bus-off decoding and restart remain pending CAN/USB capture analysis.
Keep all logs and captures outside the repository.

## Shared RX endpoint during channel transitions

Version 0.1.18 processes at most 16 RX transfers during drain while either channel remains active. It does not require the shared endpoint to become idle under continuous peer traffic/errors. Remaining events are parsed by the worker; transfers are not discarded. With both channels inactive, the strict idle check and 500 ms deadline remain in place. USB/parse failures still propagate.

The preceding 0.1.17 hardware test confirmed continued ACK retries and Error-Passive at TX counter 128, but restarting the peer failed because the shared RX drain never became idle. The 0.1.18 hardware checks above confirmed recovery in both channel directions.
