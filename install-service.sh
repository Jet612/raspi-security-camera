#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
service_user="${SUDO_USER:-$USER}"
service_group="$(id -gn "$service_user")"
template="$project_dir/deploy/raspi-security-camera.service.in"
temporary_unit="$(mktemp)"
trap 'rm -f "$temporary_unit"' EXIT

mkdir -p "$project_dir/recordings"

if ! command -v rpicam-vid >/dev/null 2>&1 && ! command -v libcamera-vid >/dev/null 2>&1; then
  echo "rpicam-vid is missing. Install it first with:"
  echo "  sudo apt update && sudo apt install rpicam-apps"
  exit 1
fi

sed \
  -e "s|__SERVICE_USER__|$service_user|g" \
  -e "s|__SERVICE_GROUP__|$service_group|g" \
  -e "s|__PROJECT_DIR__|$project_dir|g" \
  "$template" > "$temporary_unit"

sudo install -m 0644 "$temporary_unit" /etc/systemd/system/raspi-security-camera.service
sudo systemctl daemon-reload
sudo systemctl enable raspi-security-camera.service
sudo systemctl restart raspi-security-camera.service

echo
echo "Security camera service installed and started."
echo "Open http://$(hostname -I | awk '{print $1}'):8080"
echo "Status: sudo systemctl status raspi-security-camera"
echo "Logs:   journalctl -u raspi-security-camera -f"
