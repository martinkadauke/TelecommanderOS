#!/usr/bin/env bash
# Install retrokb on a Proxmox/Debian host. Idempotent; never clobbers config.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
cd "$(dirname "$0")"

echo "==> dependencies"
apt-get install -y python3-evdev

echo "==> /usr/local/bin/retrokb"
install -m 0755 retrokb.py /usr/local/bin/retrokb

echo "==> /etc/retrokb"
install -d -m 0755 /etc/retrokb
if [[ -e /etc/retrokb/retrokb.toml ]]; then
  echo "    config exists, leaving it alone (new version at retrokb.toml.dist)"
  install -m 0644 retrokb.toml /etc/retrokb/retrokb.toml.dist
else
  install -m 0644 retrokb.toml /etc/retrokb/retrokb.toml
fi

echo "==> systemd unit"
install -m 0644 retrokb.service /etc/systemd/system/retrokb.service
systemctl daemon-reload

cat <<'MSG'

Installed, but NOT started.

  1. edit  /etc/retrokb/retrokb.toml   (set the webhook URL)
  2. test  retrokb --dry-run -v        (logs payloads, does not POST)
  3. go    systemctl enable --now retrokb
     watch journalctl -fu retrokb

Note: once running, the keyboard is grabbed exclusively and stops reaching
this host's console. `systemctl stop retrokb` gives it back.
MSG
