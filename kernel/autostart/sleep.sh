#!/usr/bin/env bash
# system-sleep hook: suspend only; no calls into frozen user sessions.
set -Eeuo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
[[ ${2:-} == suspend ]] || exit 0
marker=/run/cancasexl-suspended
case ${1:-} in
pre)
    exec 9>/run/lock/cancasexl-prepare.lock
    flock -x -w 15 9
    rm -f "$marker"
    # Record only devices actually owned by this driver, not userspace devices.
    devices=()
    shopt -s nullglob
    for intf in /sys/bus/usb/drivers/cancasexl/*:*; do
        name=${intf##*/}
        devices+=("${name%%:*}")
    done
    (( ${#devices[@]} )) || exit 0
    for net in /sys/class/net/*; do
        if [[ -L $net/device/driver && $(basename "$(readlink -f "$net/device/driver")") == cancasexl ]]; then
            ip link set dev "${net##*/}" down
        fi
    done
    rmmod cancasexl
    umask 077
    printf '%s\n' "${devices[@]}" > "$marker"
    echo 'CANcase suspend: channels stopped; module unloaded'
    ;;
post)
    [[ -f $marker ]] || exit 0
    # Do not wait for a unit ordered after the currently running suspend unit.
    systemctl --no-block start cancasexl-resume.service
    ;;
esac
