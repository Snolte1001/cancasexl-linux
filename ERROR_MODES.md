# CAN error policies (0.1.19, testing)

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

Active, Warning, Passive and Bus-off firmware states are identified. A matched
Windows CAN/USB short-circuit capture confirms Bus-off status 0x01 in tag 4.
The bus-off counter bytes are not valid live error counters: the driver invalidates
the counter cache and omits CAN_ERR_CNT instead of reporting the sentinel as 127.
Linux bus-off handling and restart are implemented but still need hardware tests.

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
Reinstalling writes the managed default (continuing mode). Conflicting additional options cause installation to stop; external configuration files are not automatically modified.

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
Bus-off encoding was subsequently identified from a matched CAN/USB capture.
Linux bus-off handling and restart remain to be hardware-validated.
Keep all logs and captures outside the repository.

## Shared RX endpoint during channel transitions

Version 0.1.18 processes at most 16 RX transfers during drain while either channel remains active. It does not require the shared endpoint to become idle under continuous peer traffic/errors. Remaining events are parsed by the worker; transfers are not discarded. With both channels inactive, the strict idle check and 500 ms deadline remain in place. USB/parse failures still propagate.

The preceding 0.1.17 hardware test confirmed continued ACK retries and Error-Passive at TX counter 128, but restarting the peer failed because the shared RX drain never became idle. The 0.1.18 hardware checks above confirmed recovery in both channel directions.


## Bus-off and restart (0.1.19)

A confirmed Bus-off stops only the affected hardware channel, discards pending
transmissions and reports CAN_STATE_BUS_OFF / CAN_ERR_BUSOFF. CAN core increments
the bus-off count and controls the restart timer. Repeated or late records cannot
clear the latched state or re-arm the timer while that channel is inactive.

- `restart-ms 0` (default): no automatic restart. Remove the fault, then use
  `ip link set INTERFACE type can restart`, or explicitly cycle down/up.
- `restart-ms N`: CAN core requests restart after N milliseconds from the bus-off
  notification. This is a requested delay, not a hard real-time guarantee.
- Restart reuses the confirmed channel-start sequence under the USB I/O lock.
  Hardware success and latency require testing; a command/transport failure
  retains the existing device-wide fault handling and requires replug.
- Manual `type can restart` requires Bus-off and automatic restart disabled.
- Down and disconnect prevent pending restart work from reactivating a channel.
- Restrictive isolation may stop the channel on the first error before hardware
  reaches Bus-off. Use `isolate_can_errors=0`, `diagnose_channel=0` for these tests.
- A persistent physical fault may cause another Bus-off after restart. Pending
  frames from the previous Bus-off are not replayed by the driver.

Example configuration (bring the interface down first):

```sh
sudo ip link set can051969_1 down
sudo ip link set can051969_1 type can bitrate 500000 restart-ms 1000
sudo ip link set can051969_1 up
```

Hardware validation sequence:

1. With restart-ms 0, provoke and confirm BUS-OFF. Verify that removing the fault
   alone does not restart it and that the other channel remains operational.
2. After fault removal, request manual restart and verify bidirectional traffic,
   Error-Active, and the restart count. Also verify explicit down/up recovery.
3. Repeat with restart-ms 1000 and check delayed restart after fault removal.
4. While a longer restart delay is pending, bring the channel down; it must stay
   down. Repeat with USB unplug to check teardown and subsequent hotplug.

Keep logs and captures outside the repository. Main remains at validated 0.1.18
until these hardware checks pass and promotion is requested.
