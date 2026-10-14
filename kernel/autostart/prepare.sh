#!/usr/bin/env bash
# Per-device handoff; never unload a module serving another CANcase.
set -Eeuo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
exec 9>/run/lock/cancasexl-prepare.lock
flock -x 9
[[ ( $# == 2 || ( $# == 3 && $3 == --resume ) ) && $1 == --device ]] || exit 2
loader_args=()
[[ $# == 2 ]] || loader_args=(--resume)
device=$2
name=${device##*/}
[[ $name =~ ^[0-9]+-[0-9]+(\.[0-9]+)*$ ]] || exit 2
[[ $(readlink -f -- "$device") == "$(readlink -f -- "/sys/bus/usb/devices/$name")" ]] || exit 2
[[ $(<"$device/idVendor") == 1248 && $(<"$device/idProduct") == 1001 ]] || exit 2
bus=$(<"$device/busnum"); address=$(<"$device/devnum")
shopt -s nullglob
nets=("$device/$name:2.0/net/"*)
if [[ -L $device/$name:2.0/driver ]]; then
    if [[ $(readlink -f "$device/$name:2.0/driver") == /sys/bus/usb/drivers/cancasexl ]] && (( ${#nets[@]} == 2 )); then
        echo "status: already_bound; device: $name; no changes"
        exit 0
    fi
    echo "Target $name already owned; refusing firmware preparation" >&2
    exit 1
fi
# Loading is safe only with the probe gate closed. No global rmmod here.
modprobe cancasexl managed_handoff=1
[[ -r /sys/module/cancasexl/parameters/managed_handoff &&
   $(</sys/module/cancasexl/parameters/managed_handoff) == Y ]] || {
    echo 'Managed handoff unavailable: install current module with all CANcases unplugged' >&2
    exit 1
}
gate=/sys/module/cancasexl/parameters/bind_target
printf none > "$gate"
trap 'printf none > "$gate"' EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
/opt/cancasexl-autostart/firmware --check /opt/cancasexl-autostart/cancasexl-8.7.16.bin
[[ $(<"$device/busnum") == "$bus" && $(<"$device/devnum") == "$address" ]]
/opt/cancasexl-autostart/firmware "${loader_args[@]}" "$bus" "$address" /opt/cancasexl-autostart/cancasexl-8.7.16.bin
[[ $(<"$device/busnum") == "$bus" && $(<"$device/devnum") == "$address" ]]
# Arm only after the loader has released USB. Probe consumes this token.
printf '%d:%d' "$bus" "$address" > "$gate"
printf '%s' "$name:2.0" > /sys/bus/usb/drivers/cancasexl/bind
printf none > "$gate"
for ((attempt=0; attempt<50; attempt++)); do
 [[ $(<"$device/devnum") == "$address" ]]
 nets=("$device/$name:2.0/net/"*)
 if (( ${#nets[@]} == 2 )); then
  printf 'status: ready; C loader; channels_started: false; interfaces: %s %s\n' "${nets[0]##*/}" "${nets[1]##*/}"
  exit 0
 fi
 sleep 0.1
done
echo 'Kernel interfaces missing after preparation; inspect dmesg' >&2
exit 1
