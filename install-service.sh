#!/usr/bin/env bash
set -euo pipefail
umask 077

info() {
  printf '\n==> %s\n' "$1"
}

tailscale_serve_url() {
  sudo tailscale serve status 2>/dev/null | \
    sed -n '/^[[:space:]]*https:\/\// { s/^[[:space:]]*//; p; }' | \
    sed -n '1p'
}

die() {
  printf '\nInstallation stopped: %s\n' "$1" >&2
  exit 1
}

show_help() {
  cat <<'EOF'
Install Raspberry Pi Security Camera as a system service.

Usage: ./install-service.sh [options]

Options:
  --skip-dependencies  Do not install Raspberry Pi OS packages
  --tailscale-serve    Install/configure private HTTPS access with Tailscale Serve
  --no-tailscale-serve Skip the Tailscale question and leave its configuration alone
  --help               Show this help

The installer can safely be run again to repair the service or change its
dashboard password. Without a Tailscale option, it asks whether to configure
Tailscale Serve. It does not delete recordings or saved settings.
EOF
}

install_dependencies=true
tailscale_mode="ask"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-dependencies)
      install_dependencies=false
      ;;
    --tailscale-serve)
      tailscale_mode="enable"
      ;;
    --no-tailscale-serve)
      tailscale_mode="skip"
      ;;
    --help|-h)
      show_help
      exit 0
      ;;
    *)
      die "Unknown option: $1 (try --help)"
      ;;
  esac
  shift
done

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
service_user="${SUDO_USER:-$USER}"
if [[ "$service_user" == "root" ]]; then
  die "Run this command from your normal Raspberry Pi user, not a root shell. Using sudo is okay."
fi
if [[ ! "$service_user" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*\$?$ ]]; then
  die "The account name '$service_user' is not supported."
fi
service_group="$(id -gn "$service_user")"

if [[ "$(uname -s)" != "Linux" ]]; then
  die "This installer must be run on a Raspberry Pi using Raspberry Pi OS."
fi
if ! command -v systemctl >/dev/null 2>&1 || [[ ! -d /run/systemd/system ]]; then
  die "systemd is not running. This installer supports Raspberry Pi OS, not Docker or WSL."
fi
if ! command -v sudo >/dev/null 2>&1; then
  die "sudo is required. Install it or run this from the standard Raspberry Pi OS user account."
fi

info "Checking administrator access"
sudo -v || die "Your account needs permission to use sudo."

if [[ "$install_dependencies" == true ]]; then
  command -v apt-get >/dev/null 2>&1 || \
    die "The apt package manager was not found. Raspberry Pi OS is required."
  info "Installing camera and detection software (this can take a few minutes)"
  sudo apt-get update
  sudo apt-get install -y \
    git \
    ca-certificates \
    curl \
    rpicam-apps \
    python3-opencv \
    python3-numpy \
    openssl \
    policykit-1
fi

unit_template="$project_dir/deploy/raspi-security-camera.service.in"
update_unit_template="$project_dir/deploy/raspi-security-camera-update.service.in"
polkit_template="$project_dir/deploy/raspi-security-camera-reboot.rules.in"
credentials_dir="/etc/raspi-security-camera"
password_file="$credentials_dir/password-hash"
certificate_file="$credentials_dir/tls.crt"
private_key_file="$credentials_dir/tls.key"
environment_file="$credentials_dir/environment"
settings_file="/var/lib/raspi-security-camera/device-settings.json"

for required_file in "$unit_template" "$update_unit_template" "$polkit_template" \
    "$project_dir/update.sh"; do
  [[ -f "$required_file" ]] || die "The app download is incomplete. Run the easy installer again."
done
chmod +x "$project_dir/update.sh"

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
temporary_update_unit="$(mktemp)"
temporary_password="$(mktemp)"
temporary_certificate="$(mktemp)"
temporary_key="$(mktemp)"
temporary_environment="$(mktemp)"
temporary_polkit="$(mktemp)"
temporary_tailscale_installer="$(mktemp)"
trap 'rm -f "$temporary_unit" "$temporary_update_unit" "$temporary_password" "$temporary_certificate" "$temporary_key" "$temporary_environment" "$temporary_polkit" "$temporary_tailscale_installer"' EXIT

for command in git openssl python3 sed systemctl busctl; do
  if ! command -v "$command" >/dev/null 2>&1; then
    die "$command is required but was not found. Re-run without --skip-dependencies."
  fi
done

if ! command -v rpicam-vid >/dev/null 2>&1 && ! command -v libcamera-vid >/dev/null 2>&1; then
  die "The Raspberry Pi camera command is missing. Re-run without --skip-dependencies."
fi

if ! python3 -c 'import cv2; import numpy' >/dev/null 2>&1; then
  die "Python camera detection libraries could not be loaded. Re-run without --skip-dependencies."
fi

configure_tailscale=false
if [[ "$tailscale_mode" == "enable" ]]; then
  configure_tailscale=true
elif [[ "$tailscale_mode" == "ask" && -r /dev/tty ]]; then
  echo
  echo "Optional: Tailscale Serve gives this camera a private HTTPS address that"
  echo "works from your other Tailscale devices, even away from home."
  echo "If enabled, use the Tailscale address instead of the Pi's local IP address."
  read -r -p "Set up private remote access with Tailscale Serve? [y/N] " response </dev/tty || true
  if [[ "$response" =~ ^[yY]$ ]]; then
    configure_tailscale=true
  fi
fi

tailscale_url=""
if [[ "$configure_tailscale" == true ]]; then
  if ! command -v tailscale >/dev/null 2>&1; then
    if [[ "$install_dependencies" != true ]]; then
      die "Tailscale is not installed. Install it first or re-run without --skip-dependencies."
    fi
    command -v curl >/dev/null 2>&1 || \
      die "curl is required to install Tailscale. Re-run without --skip-dependencies."
    info "Installing Tailscale from its official installer"
    curl -fsSL --proto '=https' --tlsv1.2 \
      -o "$temporary_tailscale_installer" https://tailscale.com/install.sh || \
      die "Could not download the official Tailscale installer."
    sudo sh "$temporary_tailscale_installer" || die "Tailscale could not be installed."
  fi

  command -v tailscale >/dev/null 2>&1 || die "Tailscale was installed but its command was not found."
  sudo systemctl enable --now tailscaled || die "The Tailscale service could not be started."
  if ! sudo tailscale status >/dev/null 2>&1; then
    info "Connecting this Pi to Tailscale"
    echo "Tailscale may print a sign-in link. Open it on any device and approve this Pi."
    sudo tailscale up || die "Tailscale sign-in did not finish. Run the installer again when ready."
  fi

  info "Configuring private HTTPS access with Tailscale Serve"
  sudo tailscale serve --bg https+insecure://127.0.0.1:8080 || \
    die "Tailscale Serve could not be configured. Follow the message above, then run the installer again."
  camera_host="127.0.0.1"
  tailscale_url="$(tailscale_serve_url || true)"
fi

for device_group in video render; do
  if getent group "$device_group" >/dev/null 2>&1 && \
      ! id -nG "$service_user" | tr ' ' '\n' | grep -Fxq "$device_group"; then
    info "Giving $service_user access to camera hardware"
    sudo usermod -a -G "$device_group" "$service_user"
  fi
done

info "Creating the dashboard login and encrypted connection"
mkdir -p "$project_dir/recordings"
sudo install -d -m 0750 -o root -g "$service_group" "$credentials_dir"

replace_password=true
if sudo test -s "$password_file"; then
  replace_password=false
  if [[ -r /dev/tty ]]; then
    read -r -p "A dashboard password already exists. Replace it? [y/N] " response </dev/tty || true
  else
    response=""
  fi
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

sed \
  -e "s|__SERVICE_USER__|$service_user|g" \
  -e "s|__SERVICE_GROUP__|$service_group|g" \
  -e "s|__PROJECT_DIR__|$project_dir|g" \
  "$update_unit_template" > "$temporary_update_unit"

sed -e "s|__SERVICE_USER__|$service_user|g" \
  "$polkit_template" > "$temporary_polkit"

sudo install -m 0644 "$temporary_unit" /etc/systemd/system/raspi-security-camera.service
sudo install -m 0644 "$temporary_update_unit" /etc/systemd/system/raspi-security-camera-update.service
sudo install -m 0644 "$temporary_polkit" /etc/polkit-1/rules.d/50-raspi-security-camera-reboot.rules
info "Starting the security camera"
sudo systemctl daemon-reload
sudo systemctl enable raspi-security-camera.service
sudo systemctl restart raspi-security-camera.service

if ! sudo systemctl is-active --quiet raspi-security-camera.service; then
  echo
  echo "The service did not start. Here are its latest messages:"
  sudo journalctl -u raspi-security-camera.service -n 20 --no-pager || true
  die "Fix the error above, then run this installer again."
fi

echo
echo "Security camera installed successfully."
if [[ "$camera_host" == "127.0.0.1" ]]; then
  proxy_url="$tailscale_url"
  if [[ -z "$proxy_url" ]] && command -v tailscale >/dev/null 2>&1; then
    proxy_url="$(tailscale_serve_url || true)"
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
