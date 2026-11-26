#!/bin/bash
set -Eeuo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
trap 'echo "NEB_IMAGE_SEAL_FAILED line=$LINENO" >/dev/ttyS0; systemctl --no-block poweroff' ERR

cloud-init clean --logs
if [ -d /var/lib/cloud/seed ]; then
    find /var/lib/cloud/seed -mindepth 1 -delete
fi
find /etc/ssh -maxdepth 1 -type f -name 'ssh_host_*' -delete
systemctl stop systemd-random-seed.service
rm -f /var/lib/systemd/random-seed
truncate -s 0 /etc/machine-id
if [ -f /var/lib/dbus/machine-id ] && [ ! -L /var/lib/dbus/machine-id ]; then
    rm -f /var/lib/dbus/machine-id
fi

rm -f /run/neb-image-loader /run/apollo-neb-image-provision.sh /usr/local/libexec/apollo-neb-image-seal
sync
printf '%s\n' 'NEB_IMAGE_SEAL_OK' >/dev/ttyS0
systemctl --no-block poweroff
