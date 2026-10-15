#!/usr/bin/env bash
# Check the effective kmod configuration before loading the installed module.
set -Eeuo pipefail
config=$(modprobe --showconfig) || {
    printf 'Error: Cannot read effective modprobe configuration.\n' >&2
    exit 1
}
declare -A seen=() expected=(
    [isolate_can_errors]=0 [diagnose_channel]=0 [managed_handoff]=1
)
bad=0
while IFS= read -r line; do
    read -r -a fields <<< "$line"
    [[ ${fields[0]:-} == options && ${fields[1]:-} == cancasexl ]] || continue
    printf 'Effective module options: %s\n' "$line"
    for token in "${fields[@]:2}"; do
        [[ $token == \#* ]] && break
        [[ $token == *=* ]] || continue
        key=${token%%=*}
        value=${token#*=}
        [[ $key =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]] || { bad=1; continue; }
        case "$key" in
            isolate_can_errors|managed_handoff|fast_poll)
                case "${value,,}" in
                    1|y|yes|true) value=1 ;;
                    0|n|no|false) value=0 ;;
                esac ;;
        esac
        if [[ -v seen[$key] && ${seen[$key]} != "$value" ]]; then
            printf 'Error: Conflicting cancasexl option %s: %s versus %s.\n' \
                "$key" "${seen[$key]}" "$value" >&2
            bad=1
        fi
        seen[$key]=$value
        if [[ -v expected[$key] && ${expected[$key]} != "$value" ]]; then
            printf 'Error: cancasexl option %s=%s overrides installer default %s.\n' \
                "$key" "$value" "${expected[$key]}" >&2
            bad=1
        fi
    done
done <<< "$config"
for key in "${!expected[@]}"; do
    if [[ ! -v seen[$key] ]]; then
        printf 'Error: Missing managed cancasexl option %s.\n' "$key" >&2
        bad=1
    fi
done
if (( bad )); then
    printf 'Configuration sources (including possibly shadowed files):\n' >&2
    for dir in /etc/modprobe.d /run/modprobe.d /usr/local/lib/modprobe.d /usr/lib/modprobe.d /lib/modprobe.d; do
        [[ -d $dir ]] || continue
        grep -HnE '^[[:space:]]*options[[:space:]]+cancasexl([[:space:]]|$)' \
            "$dir"/*.conf >&2 2>/dev/null || true
    done
    printf '%s\n' \
        'Resolve conflicting entries in additional .conf files, then rerun the installer.' \
        'No external configuration files have been changed automatically.' \
        'The installer has not completed; do not reconnect devices yet.' >&2
    exit 1
fi
