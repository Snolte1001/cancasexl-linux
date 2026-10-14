# Hardware tests

Install the driver using README.md first. These scripts are optional and do not run in the driver data path.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

## Two separate buses

Connect Vector channel 1 to external SocketCAN peer `can0` at 500 kbit/s.
Connect Vector channel 2 to peer `can1` at 1 Mbit/s. Terminate both buses correctly.
Verify peer names; they can change between boots. Replace serial 123456 with your device serial number.

```sh
sudo ip link set can0 down
sudo ip link set can0 type can bitrate 500000 listen-only off restart-ms 0
sudo ip link set can0 up
sudo ip link set can1 down
sudo ip link set can1 type can bitrate 1000000 listen-only off restart-ms 0
sudo ip link set can1 up
sudo .venv/bin/python -m cancasexl.kernel_test \
  --interfaces can123456_1 can123456_2 --peers can0 can1 \
  --traffic stream --window 4 --rate 1000 --seconds 30 \
  --report "$HOME/kernel_test.json"
```

## Mixed RTR and data traffic

For this separate test, connect the two Vector channels to each other on one terminated bus.
Configure both channels to 1 Mbit/s and bring them UP before starting:

```sh
python3 tools/rtr_loadtest.py --interfaces can123456_1 can123456_2 \
  --rate 1000 --seconds 30 --window 4 --report "$HOME/rtr_test.json"
```

The script checks standard/extended identifiers, data/RTR flags, DLC and data payloads.
The requested rate is a target; consult achieved rates in the generated report.

Other test modules expose `--help`: `kernel_errors`, `kernel_filters`, and `kernel_diagnose`.
Error tests deliberately interrupt communication and require the wiring described in their options.
Python transport/bridge modules are included as dependencies of the test tools, not as a replacement for the kernel driver.

Always write generated results outside the repository. No recorded fixtures, traffic dumps or historical reports are included.

## Hardware timestamps

See [TIMESTAMPS.md](TIMESTAMPS.md) for the 0.1.16 raw RX/TX timestamp test.
