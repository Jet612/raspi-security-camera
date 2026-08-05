#!/usr/bin/env bash
set -euo pipefail

# This small bootstrap is intended for:
#   curl -fsSL https://raw.githubusercontent.com/Jet612/raspi-security-camera/main/install.sh | bash

repository_url="${RASPI_CAMERA_REPOSITORY:-https://github.com/Jet612/raspi-security-camera.git}"
install_dir="${RASPI_CAMERA_INSTALL_DIR:-${HOME}/raspi-security-camera}"

die() {
  printf '\nInstallation stopped: %s\n' "$1" >&2
  exit 1
}

if [[ "$(uname -s)" != "Linux" ]]; then
  die "Run this command on the Raspberry Pi you want to use as a camera."
fi
if [[ "${EUID}" -eq 0 ]]; then
  die "Run this command as your normal Raspberry Pi user, without 'sudo'. The installer will ask for sudo when needed."
fi
if ! command -v sudo >/dev/null 2>&1; then
  die "sudo is required. Use the standard Raspberry Pi OS user account."
fi
if ! command -v apt-get >/dev/null 2>&1; then
  die "This easy installer supports Raspberry Pi OS and its apt package manager."
fi

printf '\nRaspberry Pi Security Camera easy installer\n'
printf 'Files will be installed in: %s\n\n' "$install_dir"

if ! command -v git >/dev/null 2>&1; then
  printf 'Installing the download tool...\n'
  sudo apt-get update
  sudo apt-get install -y git ca-certificates
fi

if [[ -e "$install_dir" && ! -d "$install_dir/.git" ]]; then
  die "$install_dir already exists and is not this app. Move it elsewhere and run this command again."
fi

if [[ ! -d "$install_dir/.git" ]]; then
  printf 'Downloading the app...\n'
  git clone --depth 1 "$repository_url" "$install_dir"
else
  existing_repository="$(git -C "$install_dir" remote get-url origin 2>/dev/null || true)"
  if [[ "$existing_repository" != "$repository_url" ]]; then
    die "$install_dir belongs to a different Git repository. Move it elsewhere and run this command again."
  fi
  printf 'Using the existing app files in %s.\n' "$install_dir"
  if [[ -z "$(git -C "$install_dir" status --porcelain)" ]]; then
    printf 'Checking for app updates...\n'
    git -C "$install_dir" pull --ff-only
  else
    printf 'Local changes were found, so they were left untouched.\n'
  fi
fi

chmod +x "$install_dir/install-service.sh" "$install_dir/update.sh"
exec "$install_dir/install-service.sh"
