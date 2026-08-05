#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
restart_service=true
check_only=false

die() {
  printf '\nUpdate stopped: %s\n' "$1" >&2
  exit 1
}

show_help() {
  cat <<'EOF'
Check for and install Raspberry Pi Security Camera updates.

Usage: ./update.sh [options]

Options:
  --check       Check for an update without installing it
  --no-restart  Do not restart the camera service after updating
  --help        Show this help

Updates come from the current branch's configured Git remote. A normal fork
therefore updates from that fork's origin. Local changes are never overwritten.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)
      check_only=true
      restart_service=false
      ;;
    --no-restart)
      restart_service=false
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

command -v git >/dev/null 2>&1 || die "Git is not installed."
[[ -d "$project_dir/.git" ]] || die "This installation is not connected to a Git repository."

branch="$(git -C "$project_dir" symbolic-ref --quiet --short HEAD)" || \
  die "The app is not on a named Git branch."
remote="$(git -C "$project_dir" config --get "branch.$branch.remote")" || \
  die "The '$branch' branch has no configured remote."
merge_ref="$(git -C "$project_dir" config --get "branch.$branch.merge")" || \
  die "The '$branch' branch has no configured upstream branch."

[[ "$remote" != "." ]] || die "The branch is configured to track another local branch."
[[ "$merge_ref" == refs/heads/* ]] || die "The configured upstream branch is invalid."
git -C "$project_dir" remote get-url "$remote" >/dev/null 2>&1 || \
  die "The configured '$remote' remote does not exist."
git -C "$project_dir" check-ref-format "$merge_ref" >/dev/null 2>&1 || \
  die "The configured upstream branch is invalid."
remote_branch="${merge_ref#refs/heads/}"
remote_ref="refs/remotes/$remote/$remote_branch"
git -C "$project_dir" check-ref-format "$remote_ref" >/dev/null 2>&1 || \
  die "The configured remote tracking branch is invalid."

if [[ -n "$(git -C "$project_dir" status --porcelain --untracked-files=normal)" ]]; then
  die "Local changes were found. Commit or remove them before updating; nothing was overwritten."
fi

printf 'Checking %s/%s for updates...\n' "$remote" "$remote_branch"
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=5"
git -C "$project_dir" fetch --no-tags --prune "$remote" "+$merge_ref:$remote_ref"

current="$(git -C "$project_dir" rev-parse HEAD)"
latest="$(git -C "$project_dir" rev-parse "$remote_ref")"
if [[ "$current" == "$latest" ]]; then
  printf 'The app is already up to date (%s).\n' "${current:0:12}"
  exit 0
fi
if ! git -C "$project_dir" merge-base --is-ancestor "$current" "$latest"; then
  if git -C "$project_dir" merge-base --is-ancestor "$latest" "$current"; then
    die "The local branch has commits that are not on the remote. Update manually so no work is lost."
  fi
  die "The local and remote branches have diverged. Update manually so no work is lost."
fi
if [[ "$check_only" == true ]]; then
  printf 'An update is available: %s -> %s\n' "${current:0:12}" "${latest:0:12}"
  exit 0
fi

git -C "$project_dir" merge --ff-only "$remote_ref"
printf '\nUpdated successfully: %s -> %s\n' "${current:0:12}" "${latest:0:12}"

if [[ "$restart_service" == true ]] && command -v systemctl >/dev/null 2>&1 && \
    systemctl cat raspi-security-camera.service >/dev/null 2>&1; then
  printf 'Restarting the camera service...\n'
  sudo systemctl restart raspi-security-camera.service
fi

printf 'Update complete.\n'
