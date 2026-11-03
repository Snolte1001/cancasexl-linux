#!/usr/bin/env bash
# Called by install-autostart.sh after firmware validation and USB absence check.
set -Eeuo pipefail
KERNEL_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
DEST=/opt/cancasexl-autostart
die() { printf 'DKMS: %s\n' "$*" >&2; exit 1; }
# Read the package version without executing dkms.conf.
VERSION=$(sed -n 's/^PACKAGE_VERSION="\([0-9][0-9.]*\)"$/\1/p' "$KERNEL_DIR/dkms.conf")
[[ $VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
    die 'Expected exactly one numeric PACKAGE_VERSION in dkms.conf.'
MODULE_VERSION=$(sed -n 's/^MODULE_VERSION("\([0-9][0-9.]*\)");$/\1/p' "$KERNEL_DIR/cancasexl.c")
[[ $MODULE_VERSION == "$VERSION" ]] ||
    die 'MODULE_VERSION and PACKAGE_VERSION differ; refusing installation.'
SOURCE="/usr/src/cancasexl-$VERSION"
[[ $EUID == 0 ]] || die 'Please run with sudo.'
command -v dkms >/dev/null || die 'Install DKMS first: sudo apt install dkms.'
[[ -f $DEST/.cancasexl-managed && ! -L $DEST ]] || die 'Run install-autostart.sh first.'
[[ ! -d /sys/module/cancasexl ]] || die 'Unload the module before installing with DKMS.'
for dev in /sys/bus/usb/devices/*; do
    if [[ -f $dev/idVendor && -f $dev/idProduct ]] &&
       [[ $(<"$dev/idVendor") == 1248 && $(<"$dev/idProduct") == 1001 ]]; then
        die 'Unplug all CANcase XL devices before installation.'
    fi
done
[[ -d /lib/modules/$(uname -r)/build ]] || die 'Matching kernel headers are missing.'
[[ ! -L $SOURCE ]] || die 'Source directory must not be a symbolic link.'
if [[ -e $SOURCE ]]; then
    [[ -f $SOURCE/.cancasexl-managed ]] || die 'Refusing to overwrite unmanaged source files.'
    for file in cancasexl.c license_decode.h Makefile dkms.conf; do
        cmp -s "$KERNEL_DIR/$file" "$SOURCE/$file" ||
            die "Sources for version $VERSION differ. Refusing to overwrite the same version."
    done
else
    install -d -m 0755 "$SOURCE"
    for file in cancasexl.c license_decode.h Makefile dkms.conf; do
        install -m 0644 "$KERNEL_DIR/$file" "$SOURCE/$file"
    done
    install -m 0644 /dev/null "$SOURCE/.cancasexl-managed"
fi
# Keep a managed-version record even if building fails, for clean removal.
if ! grep -qxF "$VERSION" "$DEST/dkms-versions" 2>/dev/null; then
    printf '%s\n' "$VERSION" >> "$DEST/dkms-versions"
fi
if [[ ! -e /var/lib/dkms/cancasexl/$VERSION/source ]]; then
    dkms add -m cancasexl -v "$VERSION"
fi
# Build before changing installed modules. DKMS backs up a pre-existing manual
# module as original_module. --force permits migration from the same version.
dkms build -m cancasexl -v "$VERSION" -k "$(uname -r)"
dkms install -m cancasexl -v "$VERSION" -k "$(uname -r)" --force
depmod -a "$(uname -r)"
[[ $(modinfo -F version cancasexl) == "$VERSION" ]] || die 'Installed module version does not match.'
[[ $(modinfo -F vermagic cancasexl) == "$(uname -r) "* ]] || die 'Installed module does not match the kernel.'
dkms status -m cancasexl -v "$VERSION" -k "$(uname -r)"
printf 'DKMS module path: %s\n' "$(modinfo -n cancasexl)"

