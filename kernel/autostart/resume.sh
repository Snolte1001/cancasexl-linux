#!/usr/bin/env bash
set -Eeuo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
marker=/run/cancasexl-suspended
[[ -f $marker ]] || exit 0
mapfile -t ports < "$marker"
for port in "${ports[@]}"; do
    [[ $port =~ ^[0-9]+-[0-9]+(\.[0-9]+)*$ ]] || exit 1
done
failed=0
for port in "${ports[@]}"; do
    found=0
    # Wait only for enumeration; one firmware attempt per device.
    for ((attempt=0; attempt<50; attempt++)); do
        device="/sys/bus/usb/devices/$port"
        if [[ -f $device/idVendor && -f $device/idProduct ]] &&
           [[ $(<"$device/idVendor") == 1248 && $(<"$device/idProduct") == 1001 ]]; then
            found=1
            # Launch a separate shell: errexit inside prepare remains effective.
            if /bin/bash /opt/cancasexl-autostart/prepare.sh --device "$(readlink -f "$device")" --resume; then
                echo "CANcase resume: $port ready; channels remain DOWN"
            else
                echo "CANcase resume: $port failed; continuing with other devices" >&2
                failed=1
            fi
            break
        fi
        sleep 0.1
    done
    (( found )) || echo "CANcase resume: $port absent; next USB add uses normal autostart"
done
rm -f "$marker"
exit "$failed"
