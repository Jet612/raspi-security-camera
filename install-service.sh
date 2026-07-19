#!/usr/bin/env bash
set -euo pipefail
umask 077

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
service_user="${SUDO_USER:-$USER}"
if [[ "$service_user" == "root" ]]; then
  echo "Refusing to run the camera service as root. Run this installer from the device user account."
  exit 1
fi
if [[ ! "$service_user" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*\$?$ ]]; then
  echo "Unsupported service username: $service_user"
  exit 1
fi
service_group="$(id -gn "$service_user")"

unit_template="$project_dir/deploy/raspi-security-camera.service.in"
polkit_template="$project_dir/deploy/raspi-security-camera-reboot.rules.in"
credentials_dir="/etc/raspi-security-camera"
password_file="$credentials_dir/password-hash"
certificate_file="$credentials_dir/tls.crt"
private_key_file="$credentials_dir/tls.key"
environment_file="$credentials_dir/environment"
settings_file="/var/lib/raspi-security-camera/device-settings.json"

# Preserve loopback-only mode once a reverse proxy has been configured. A fresh
# direct-LAN installation still binds on all interfaces with its generated TLS key.
camera_host="0.0.0.0"
if sudo test -f "$environment_file"; then
  existing_host="$(sudo sed -n 's/^CAMERA_HOST=//p' "$environment_file" | tail -n 1)"
  if [[ "$existing_host" == "127.0.0.1" ]]; then
    camera_host="$existing_host"
  fi
fi

temporary_unit="$(mktemp)"
temporary_password="$(mktemp)"
temporary_certificate="$(mktemp)"
temporary_key="$(mktemp)"
temporary_environment="$(mktemp)"
temporary_polkit="$(mktemp)"
trap 'rm -f "$temporary_unit" "$temporary_password" "$temporary_certificate" "$temporary_key" "$temporary_environment" "$temporary_polkit"' EXIT

for command in openssl python3 sed systemctl busctl; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "$command is required but was not found."
    exit 1
  fi
done

if ! command -v rpicam-vid >/dev/null 2>&1 && ! command -v libcamera-vid >/dev/null 2>&1; then
  echo "rpicam-vid is missing. Install it first with:"
  echo "  sudo apt update && sudo apt install rpicam-apps"
  exit 1
fi

mkdir -p "$project_dir/recordings"
sudo install -d -m 0750 -o root -g "$service_group" "$credentials_dir"

replace_password=true
if sudo test -s "$password_file"; then
  replace_password=false
  read -r -p "A dashboard password already exists. Replace it? [y/N] " response
  if [[ "$response" =~ ^[yY]$ ]]; then
    replace_password=true
  fi
fi
if [[ "$replace_password" == true ]]; then
  password_hash="$(python3 "$project_dir/camera_server.py" --hash-password)"
  printf '%s\n' "$password_hash" > "$temporary_password"
  sudo install -m 0640 -o root -g "$service_group" "$temporary_password" "$password_file"
else
  sudo chown root:"$service_group" "$password_file"
  sudo chmod 0640 "$password_file"
fi

hostname_value="$(hostname)"
if [[ ! "$hostname_value" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]]; then
  echo "The device hostname cannot be used in a TLS certificate: $hostname_value"
  exit 1
fi
primary_ip="$(hostname -I | awk '{print $1}')"
subject_alt_names="DNS:$hostname_value,DNS:$hostname_value.local"
if [[ "$primary_ip" =~ ^[0-9a-fA-F:.]+$ ]]; then
  subject_alt_names="$subject_alt_names,IP:$primary_ip"
fi

replace_certificate=true
if sudo test -s "$certificate_file" && sudo test -s "$private_key_file"; then
  if sudo openssl x509 -checkend 2592000 -noout -in "$certificate_file" >/dev/null 2>&1; then
    replace_certificate=false
  fi
fi
if [[ "$replace_certificate" == true ]]; then
  openssl req -x509 -newkey rsa:3072 -sha256 -days 825 -nodes \
    -subj "/CN=$hostname_value" \
    -addext "subjectAltName=$subject_alt_names" \
    -keyout "$temporary_key" -out "$temporary_certificate" >/dev/null 2>&1
  sudo install -m 0644 -o root -g root "$temporary_certificate" "$certificate_file"
  sudo install -m 0640 -o root -g "$service_group" "$temporary_key" "$private_key_file"
else
  sudo chown root:"$service_group" "$private_key_file"
  sudo chmod 0640 "$private_key_file"
fi

printf '%s\n' \
  "CAMERA_HOST=$camera_host" \
  "CAMERA_PASSWORD_HASH=" \
  "CAMERA_PASSWORD_FILE=$password_file" \
  "CAMERA_TLS_CERT=$certificate_file" \
  "CAMERA_TLS_KEY=$private_key_file" \
  "CAMERA_SETTINGS_FILE=$settings_file" \
  "CAMERA_TRUST_PROXY_HTTPS=false" \
  > "$temporary_environment"
sudo install -m 0640 -o root -g "$service_group" "$temporary_environment" "$environment_file"

sed \
  -e "s|__SERVICE_USER__|$service_user|g" \
  -e "s|__SERVICE_GROUP__|$service_group|g" \
  -e "s|__PROJECT_DIR__|$project_dir|g" \
  "$unit_template" > "$temporary_unit"

sed -e "s|__SERVICE_USER__|$service_user|g" \
  "$polkit_template" > "$temporary_polkit"

sudo install -m 0644 "$temporary_unit" /etc/systemd/system/raspi-security-camera.service
sudo install -m 0644 "$temporary_polkit" /etc/polkit-1/rules.d/50-raspi-security-camera-reboot.rules
sudo systemctl daemon-reload
sudo systemctl enable raspi-security-camera.service
sudo systemctl restart raspi-security-camera.service

echo
echo "Security camera service installed and started."
if [[ "$camera_host" == "127.0.0.1" ]]; then
  proxy_url=""
  if command -v tailscale >/dev/null 2>&1; then
    proxy_url="$(tailscale serve status 2>/dev/null | sed -n '1p' || true)"
  fi
  if [[ "$proxy_url" == https://* ]]; then
    echo "Open:        $proxy_url"
  else
    echo "Open:        use the configured HTTPS reverse-proxy URL"
  fi
  echo "Backend:     https://127.0.0.1:8080 (localhost only)"
else
  viewer_host="${primary_ip:-$hostname_value}"
  echo "Open:        https://$viewer_host:8080"
fi
echo "Username:    admin (override CAMERA_USERNAME in /etc/default/raspi-security-camera)"
echo "Certificate: self-signed; verify this SHA-256 fingerprint before trusting it:"
sudo openssl x509 -in "$certificate_file" -noout -fingerprint -sha256
echo "Status:      sudo systemctl status raspi-security-camera"
echo "Logs:        journalctl -u raspi-security-camera -f"
