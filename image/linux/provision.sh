#!/bin/bash
# This runs only inside the isolated Debian image builder.
set -Eeuo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 022
trap 'echo "NEB_IMAGE_BUILD_FAILED line=$LINENO" >/dev/ttyS0; systemctl --no-block poweroff' ERR

test "$(ls /sys/class/net)" = lo
test "$(df --output=size -B1 / | tail -n 1)" -gt 7516192768
medium=$(blkid -L CIDATA)
mount_dir=/mnt/apollo-neb-seed
install -d "$mount_dir"
mount -o ro "$medium" "$mount_dir"
(
    cd "$mount_dir"
    sha256sum --strict --check files.sha256
)

if ! dpkg -s dbus-user-session 2>/dev/null | grep -qx 'Status: install ok installed'; then
    dpkg -i "$mount_dir/user-bus.deb"
fi
if getent passwd neb >/dev/null; then
    printf '%s\n' 'The base image must have no neb account.' >&2
    exit 1
fi
adduser --disabled-password --gecos '' neb
for group in video render sudo; do
    if getent group "$group" >/dev/null; then adduser neb "$group"; fi
done
passwd -l root >/dev/null
install -d -m0755 /opt/apollo-neb-image/package /usr/local/libexec
install -d -m0700 /var/lib/apollo-neb-image
tar -xzf "$mount_dir/apollo-neb.tar.gz" --strip-components=1 --no-same-owner -C /opt/apollo-neb-image/package
install -m0755 "$mount_dir/console-setup.sh" /usr/local/libexec/apollo-neb-image-console
install -m0700 "$mount_dir/seal.sh" /usr/local/libexec/apollo-neb-image-seal

install -d /etc/systemd/system/getty@tty1.service.d /etc/systemd/system/serial-getty@ttyS0.service.d
cat > /etc/systemd/system/getty@tty1.service.d/apollo-neb-image.conf <<'EOF'
[Service]
ExecStart=
ExecStart=-/usr/local/libexec/apollo-neb-image-console -o '-p -- \\u' --noclear - ${TERM}
EOF
cat > /etc/systemd/system/serial-getty@ttyS0.service.d/apollo-neb-image.conf <<'EOF'
[Service]
ExecStart=
ExecStart=-/usr/local/libexec/apollo-neb-image-console -o '-p -- \\u' --keep-baud 115200,57600,38400,9600 - ${TERM}
EOF

test -x /usr/sbin/sshd
install -d -m0755 /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/00-apollo-neb-image.conf <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AllowUsers neb
AllowAgentForwarding no
AllowTcpForwarding local
PermitOpen 127.0.0.1:18763
EOF
systemctl disable --now ssh.service >/dev/null 2>&1
systemctl mask ssh.service >/dev/null 2>&1
touch /etc/cloud/cloud-init.disabled
umount "$mount_dir"
rmdir "$mount_dir"

install -d -m0755 /etc/systemd/network
cat > /etc/systemd/network/80-apollo-neb.network <<'EOF'
[Match]
Name=en* eth*

[Link]
RequiredForOnline=no

[Network]
DHCP=yes
EOF
systemctl disable systemd-networkd-wait-online.service >/dev/null 2>&1

systemd-run --no-block --unit=apollo-neb-image-seal --property=After=cloud-final.service \
    /usr/local/libexec/apollo-neb-image-seal
