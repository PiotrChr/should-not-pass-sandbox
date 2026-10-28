#!/usr/bin/env bash
# Runs as guest root after either cloud-init or the VirtualBox unattended installer.
# SNP_USER and SNP_PUBLIC_KEY are supplied by the shared host provisioner.
set -euo pipefail
snp_guest_home=$(getent passwd "$SNP_USER" | cut -d: -f6)
snp_guest_group=$(id -gn "$SNP_USER")
test -n "$snp_guest_home"
install -d -m700 -o "$SNP_USER" -g "$snp_guest_group" "$snp_guest_home/.ssh"
printf '%s\n' "$SNP_PUBLIC_KEY" > "$snp_guest_home/.ssh/authorized_keys"
chown "$SNP_USER:$snp_guest_group" "$snp_guest_home/.ssh/authorized_keys"
chmod 600 "$snp_guest_home/.ssh/authorized_keys"
if ! command -v sshd >/dev/null; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y openssh-server
fi
ssh-keygen -A
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n' > /etc/ssh/sshd_config.d/10-snp.conf
printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$SNP_USER" > "/etc/sudoers.d/$SNP_USER"
chmod 440 "/etc/sudoers.d/$SNP_USER"
systemctl enable ssh
systemctl restart ssh
