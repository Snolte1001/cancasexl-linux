#!/usr/bin/env bash
# Install firmware autostart and register the kernel module with DKMS.
set -Eeuo pipefail
KERNEL_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
DEST=/opt/cancasexl-autostart
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
[[ $EUID == 0 ]] || die 'Run with sudo bash kernel/install-autostart.sh /path/to/vcasexl.sys.'
[[ $# -le 1 ]] || die 'Optionally specify one SYS or BIN file.'
FIRMWARE_INPUT=${1:-"$KERNEL_DIR/firmware/cancasexl-8.7.16.bin"}
[[ -f $FIRMWARE_INPUT ]] || die 'Firmware missing; supply a SYS or verified BIN file.' 
[[ -d /run/systemd/system ]] || die 'A running systemd instance is required.'
for tool in ip dkms cmp cc make modinfo depmod modprobe rmmod systemctl udevadm install flock readlink; do
    command -v "$tool" >/dev/null || die "Missing tool: $tool"
done
[[ -d /lib/modules/$(uname -r)/build ]] || die 'Install matching linux-headers.'
if [[ -e /usr/local/bin/cancasexl-info || -L /usr/local/bin/cancasexl-info ]]; then
    [[ -L /usr/local/bin/cancasexl-info && $(readlink /usr/local/bin/cancasexl-info) == "$DEST/cancasexl-info" ]] || die 'Refusing to overwrite unmanaged /usr/local/bin/cancasexl-info.'
fi
# Diagnose the common fresh-host dependency before any installed state changes.
printf '#include <openssl/evp.h>\n' | cc -x c -E - >/dev/null 2>&1 ||
    die 'OpenSSL development headers missing. Ubuntu/Debian: sudo apt install libssl-dev'
# Build the native loader before any installed state is changed.
cc -std=c11 -O2 -Wall -Wextra -Werror "$KERNEL_DIR/autostart/firmware.c" -lcrypto -o "$KERNEL_DIR/autostart/firmware"
cc -std=c11 -O2 -Wall -Wextra -Werror "$KERNEL_DIR/autostart/report-export.c" -o "$KERNEL_DIR/autostart/report-export"
"$KERNEL_DIR/autostart/firmware" --check "$FIRMWARE_INPUT"
# Update only with the Vector physically disconnected. This also avoids running
# a previous service while its installed files are replaced.
for dev in /sys/bus/usb/devices/*; do
    if [[ -f $dev/idVendor && -f $dev/idProduct ]] &&
       [[ $(<"$dev/idVendor") == 1248 && $(<"$dev/idProduct") == 1001 ]]; then
        die 'Unplug all CANcase XL devices before installation/update. Other CAN adapters may remain connected.'
    fi
done
[[ ! -L $DEST ]] || die 'Installation destination must not be a symbolic link.'
if [[ -e $DEST && ! -f $DEST/.cancasexl-managed ]]; then
    die 'Destination exists without the installer marker; refusing to overwrite.'
fi
# An earlier manual insmod need not have an entry in modules.dep yet.
# rmmod addresses the loaded module directly; never force removal.
if [[ -d /sys/module/cancasexl ]]; then rmmod cancasexl; fi
install -d -m 0755 "$DEST/lib/cancasexl" /etc/systemd/system /etc/udev/rules.d /etc/modprobe.d
install -m 0644 /dev/null "$DEST/.cancasexl-managed"
bash "$KERNEL_DIR/install-dkms.sh"
install -m 0755 "$KERNEL_DIR/autostart/firmware" "$DEST/firmware"
install -m 0755 "$KERNEL_DIR/autostart/prepare.sh" "$DEST/prepare.sh"
install -m 0755 "$KERNEL_DIR/autostart/report-export" "$DEST/report-export"
install -m 0755 "$KERNEL_DIR/autostart/cancasexl-info" "$DEST/cancasexl-info"
install -d -m 0755 /usr/local/bin
ln -sfn "$DEST/cancasexl-info" /usr/local/bin/cancasexl-info
# Validate/extract before replacing the installed image; no SYS needed at runtime.
FIRMWARE_STAGE=$(mktemp -d "$DEST/.firmware-stage.XXXXXX")
trap 'rmdir "$FIRMWARE_STAGE" 2>/dev/null || true' EXIT
"$KERNEL_DIR/autostart/firmware" --extract "$FIRMWARE_INPUT" "$FIRMWARE_STAGE/runtime.bin"
chmod 0644 "$FIRMWARE_STAGE/runtime.bin"
mv -f -- "$FIRMWARE_STAGE/runtime.bin" "$DEST/cancasexl-8.7.16.bin"
rmdir "$FIRMWARE_STAGE"
trap - EXIT
install -m 0644 "$KERNEL_DIR/autostart/cancasexl-autostart.conf" /etc/modprobe.d/
# Check that module loading works before enabling the USB trigger.
modprobe cancasexl
modprobe -r cancasexl
install -m 0644 "$KERNEL_DIR/autostart/cancasexl-prepare@.service" /etc/systemd/system/
install -m 0644 "$KERNEL_DIR/autostart/80-cancasexl-kernel.rules" /etc/udev/rules.d/
install -m 0755 "$KERNEL_DIR/autostart/resume.sh" "$DEST/resume.sh"
install -m 0644 "$KERNEL_DIR/autostart/cancasexl-resume.service" /etc/systemd/system/
install -d -m 0755 /usr/lib/systemd/system-sleep
install -m 0755 "$KERNEL_DIR/autostart/sleep.sh" /usr/lib/systemd/system-sleep/cancasexl
systemctl daemon-reload
udevadm control --reload-rules
printf '\nAutostart installed. Connect the CANcase XL now.\n'
printf 'Status: sudo journalctl -b -u "cancasexl-prepare@*" --no-pager -n 60\n'
printf 'Interfaces: ip -details link show type can\n'
printf 'Both Vector channels initially remain DOWN. See README.md.\n'
printf 'DKMS configured: rebuild on kernel updates with matching headers.\n'
printf 'Check: dkms status -m cancasexl; modinfo -n cancasexl\n'
