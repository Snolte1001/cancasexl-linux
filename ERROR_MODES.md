# CAN error policies (0.1.18, testing)

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

## Initial hardware validation

Hardware validation is pending. Use two connected, terminated channels of one
CANcase at 500 kbit/s with no additional node providing acknowledgements.

1. Verify ordinary traffic and version 0.1.18, isolation N, diagnosis 0.
2. Stop channel 2, send one frame on channel 1 and wait at least eight seconds.
3. Inspect channel 1 state/counters. A known TX error should not cause a driver
   stop or a global five-second timeout.
4. Start channel 2. Check whether the pending frame arrives and subsequent frames
   pass. If it stays blocked or stops, inspect the journal before channel 1 down/up.
5. Repeat in restrictive mode: channel 1 should stop at the first CAN error and
   require down/up after acknowledgements become available again.

Save test outputs outside this repository. Main remains at the tested 0.1.16.

## Shared RX endpoint during channel transitions

Version 0.1.18 processes at most 16 RX transfers during drain while either channel remains active. It does not require the shared endpoint to become idle under continuous peer traffic/errors. Remaining events are parsed by the worker; transfers are not discarded. With both channels inactive, the strict idle check and 500 ms deadline remain in place. USB/parse failures still propagate.

The preceding 0.1.17 hardware test confirmed continued ACK retries and Error-Passive at TX counter 128, but restarting the peer failed because the shared RX drain never became idle. Recovery with this correction remains to be hardware-tested.
