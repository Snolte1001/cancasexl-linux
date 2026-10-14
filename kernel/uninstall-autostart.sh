#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID == 0 ]] || { echo 'Please run with sudo.' >&2; exit 1; }
rm -f /usr/lib/systemd/system-sleep/cancasexl
systemctl stop cancasexl-resume.service || true
rm -f /etc/systemd/system/cancasexl-resume.service /run/cancasexl-suspended
# Disable future starts first; do not stop a CAN session or unload its module.
rm -f /etc/udev/rules.d/80-cancasexl-kernel.rules
udevadm control --reload-rules
if systemctl list-units --all --no-legend 'cancasexl-prepare@*' | grep -Eq 'activating|deactivating'; then
    echo 'Preparation is still running. Retry uninstall after completion; the trigger has already been removed.' >&2
    exit 1
fi
rm -f /etc/systemd/system/cancasexl-prepare@.service
rm -f /etc/modprobe.d/cancasexl-autostart.conf
systemctl daemon-reload
if [[ -f /opt/cancasexl-autostart/.cancasexl-managed && ! -L /opt/cancasexl-autostart ]]; then
    # Remove only DKMS versions recorded by this installer. DKMS may restore
    # an original manual module; the legacy list below removes that copy too.
    if [[ -f /opt/cancasexl-autostart/dkms-versions ]]; then
        command -v dkms >/dev/null || { echo 'DKMS is missing; reinstall it before uninstalling.' >&2; exit 1; }
        while IFS= read -r version; do
            [[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || continue
            source_dir="/usr/src/cancasexl-$version"
            if [[ -d /var/lib/dkms/cancasexl/$version ]]; then
                dkms remove -m cancasexl -v "$version" --all
            fi
            if [[ -d $source_dir && ! -L $source_dir && -f $source_dir/.cancasexl-managed ]]; then
                rm -rf -- "$source_dir"
            fi
        done < /opt/cancasexl-autostart/dkms-versions
    fi
    # Only kernel versions recorded by this installer.
    if [[ -f /opt/cancasexl-autostart/installed-kernels ]]; then
        while IFS= read -r version; do
            [[ $version =~ ^[A-Za-z0-9._+-]+$ ]] || continue
            module="/lib/modules/$version/extra/cancasexl.ko"
            [[ -f $module ]] || continue
            rm -f -- "$module"
            depmod -a "$version"
        done < /opt/cancasexl-autostart/installed-kernels
    fi
    if [[ -L /usr/local/bin/cancasexl-info && $(readlink /usr/local/bin/cancasexl-info) == /opt/cancasexl-autostart/cancasexl-info ]]; then
        rm -- /usr/local/bin/cancasexl-info
    fi
    rm -rf -- /opt/cancasexl-autostart
fi
echo 'Autostart removed. A loaded module remains active until manual unloading or reboot.'
