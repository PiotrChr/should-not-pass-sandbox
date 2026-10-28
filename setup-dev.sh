#!/usr/bin/env bash
# Run inside the Ubuntu VM after install: bash setup-dev.sh
set -euo pipefail

ARCH=$(dpkg --print-architecture)
case "$ARCH" in
  amd64|arm64) ;;
  *) echo "unsupported guest architecture: $ARCH" >&2; exit 1 ;;
esac
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
chmod 755 "$T"  # apt's _apt user must be able to read downloaded packages
APT() { sudo env DEBIAN_FRONTEND=noninteractive apt-get "$@"; }

APT update
APT install -y nodejs npm python3 python3-pip python3-venv curl git ca-certificates

# QEMU starts from a cloud image. Add a desktop for its native Cocoa VM window.
if [ "${SNP_DESKTOP:-0}" = 1 ]; then
  APT install -y --no-install-recommends ubuntu-desktop-minimal
  sudo systemctl set-default graphical.target
fi

# Chrome
curl -fL --retry 3 -o "$T/chrome.deb" "https://dl.google.com/linux/direct/google-chrome-stable_current_${ARCH}.deb"
APT install -y "$T/chrome.deb"

# Insomnia
if [ "$ARCH" = amd64 ]; then
  sudo snap install insomnia
else
  echo "Insomnia: skipped (no official Linux ARM64 desktop package); curl is available."
fi

# VS Code: keep the existing x86 snap install; use the ARM64 package on Mac guests.
if [ "$ARCH" = amd64 ]; then
  sudo snap install code --classic
else
  curl -fL --retry 3 -o "$T/code.deb" "https://update.code.visualstudio.com/latest/linux-deb-arm64/stable"
  echo 'code code/add-microsoft-repo boolean true' | sudo debconf-set-selections
  APT install -y "$T/code.deb"
fi

# Docker (log out and in for the group to apply)
APT install -y docker.io docker-compose-v2
sudo usermod -aG docker "$USER"

# Claude Code CLI (native installer, lands in ~/.local/bin)
curl -fsSL https://claude.ai/install.sh | bash
grep -q '.local/bin' ~/.bashrc || echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc

node -v
python3 --version
google-chrome --version
if [ "$ARCH" = amd64 ]; then snap list insomnia; else code --version; fi
"$HOME/.local/bin/claude" --version
