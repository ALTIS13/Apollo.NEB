#!/bin/bash
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077

ready=/var/lib/apollo-neb-image/ready
bundle=/opt/apollo-neb-image/package
owner=neb
home=/home/neb

login_console() {
    exec 9>&-
    exec /sbin/agetty "$@"
}

if [[ -e $ready ]]; then login_console "$@"; fi
printf '\nApollo.NEB\nPress Enter to set up this node.\n'
IFS= read -r

mkdir -p /run/apollo-neb-image
exec 9>/run/apollo-neb-image/console.lock
if ! flock -n 9; then
    printf '%s\n' 'Setup is already open on another console.'
    login_console "$@"
fi
if [[ -e $ready ]]; then login_console "$@"; fi

printf '%s\n' 'DevFee: 1.1% of active mining time. Mining begins only when you start a worker.'
printf '%s' 'Set up this node [yes/no]: '
IFS= read -r consent
if [[ $consent != yes ]]; then login_console "$@"; fi

uid=$(id -u "$owner")
runtime=/run/user/"$uid"
as_owner() {
    local tty_options=()
    if [[ $1 == tty ]]; then tty_options=(--pty); shift; fi
    runuser "${tty_options[@]}" -u "$owner" -- env -i HOME="$home" USER="$owner" LOGNAME="$owner" TERM="${TERM:-linux}" \
        PATH="$PATH" XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS=unix:path="$runtime/bus" "$@" 9>&-
}

loginctl enable-linger "$owner"
systemctl start "user@$uid.service"
for ((try=0; try<20; try++)); do
    if [[ -S $runtime/bus ]]; then break; fi
    sleep .25
done
test -S "$runtime/bus"

if ! as_owner systemctl --user is-active --quiet apollo-neb.service; then
    args=(--service)
    if ! as_owner python3 -I -c '
import json
from pathlib import Path
path = Path.home() / ".config/apollo-neb/fleet-link.json"
raise SystemExit(0 if path.is_file() and json.loads(path.read_bytes()).get("paired") is True else 1)
'; then
        printf '%s' 'Central HTTPS panel address (Enter for standalone): '
        IFS= read -r origin
        if [[ -n $origin ]]; then
            if [[ ! $origin =~ ^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$ ]]; then
                printf '%s\n' 'Enter an HTTPS origin without a path.'
                exit 2
            fi
            args+=(--join "$origin")
            printf '%s' 'Allow updates for this node from the panel [yes/no]: '
            IFS= read -r updates
            if [[ $updates == yes ]]; then args+=(--allow-upgrades); fi
        fi
    fi
    printf '%s\n' 'Set a password for the Apollo.NEB web panel (at least 12 characters).'
    as_owner tty /bin/sh "$bundle/install.sh" "${args[@]}"
fi

if [[ $(passwd -S "$owner" | cut -d ' ' -f 2) != P ]]; then
    printf '%s\n' 'Set a separate password for the local Linux account neb.'
    passwd "$owner"
fi

while :; do
    printf '%s' 'Paste an SSH public key for remote access (Enter keeps SSH disabled): '
    IFS= read -rs key
    printf '\n'
    if [[ -z $key ]]; then break; fi
    if (( ${#key} > 8192 )) || [[ ! $key =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521))[[:space:]] ]]; then
        printf '%s\n' 'Enter a single SSH public key.'
        continue
    fi
    if ! printf '%s\n' "$key" | ssh-keygen -lf /dev/stdin >/dev/null 2>&1; then
        printf '%s\n' 'The SSH public key was refused.'
        continue
    fi
    printf '%s\n' "$key" | as_owner /bin/sh -c '
        umask 077
        IFS= read -r key
        mkdir -p "$HOME/.ssh"
        keys="$HOME/.ssh/authorized_keys"
        if ! grep -Fqx -- "$key" "$keys" 2>/dev/null; then printf "%s\n" "$key" >> "$keys"; fi
    '
    unset key
    ssh-keygen -A >/dev/null
    systemctl unmask ssh.service
    systemctl enable --now ssh.service >/dev/null
    printf '%s\n' 'SSH is available with the supplied key. Forward 127.0.0.1:18763 to open the panel remotely.'
    break
done

: > "$ready"
sync -f "$ready"
printf '%s\n' 'Apollo.NEB setup complete. Open http://127.0.0.1:18763 through your local session or SSH tunnel.'
printf '%s\n' 'Press Enter for the Linux login prompt.'
IFS= read -r
login_console "$@"
