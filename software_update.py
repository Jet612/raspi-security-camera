"""Read-only, fork-aware Git update discovery."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit


class SoftwareUpdater:
    """Checks the current branch's configured Git remote without changing files."""

    def __init__(self, project_dir: str | Path, cache_seconds: float = 900.0) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._cached_at = 0.0
        self._cached: dict[str, object] | None = None

    def status(self, *, force: bool = False) -> dict[str, object]:
        now = time.monotonic()
        with self._lock:
            if (
                not force
                and self._cached is not None
                and now - self._cached_at < self.cache_seconds
            ):
                return dict(self._cached)
            result = self._inspect()
            self._cached = result
            self._cached_at = time.monotonic()
            return dict(result)

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None
            self._cached_at = 0.0

    def _git(self, *arguments: str, timeout: float = 10.0) -> str:
        git = shutil.which("git")
        if git is None:
            raise RuntimeError("Git is not installed")
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=5",
            }
        )
        try:
            result = subprocess.run(
                [git, "-C", str(self.project_dir), *arguments],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("The update check could not reach Git") from exc
        if result.returncode:
            raise RuntimeError("The configured Git repository could not be checked")
        return result.stdout.strip()

    def _inspect(self) -> dict[str, object]:
        base: dict[str, object] = {
            "supported": False,
            "available": False,
            "can_update": False,
            "state": "unavailable",
            "message": "Updates are unavailable for this installation.",
        }
        if not (self.project_dir / ".git").exists():
            base["message"] = "This installation is not connected to a Git repository."
            return base
        try:
            branch = self._git("symbolic-ref", "--quiet", "--short", "HEAD")
            current = self._git("rev-parse", "HEAD")
            remote = self._git("config", "--get", f"branch.{branch}.remote")
            merge_ref = self._git("config", "--get", f"branch.{branch}.merge")
            if not remote or remote == "." or not merge_ref.startswith("refs/heads/"):
                raise RuntimeError("The current branch has no remote branch")
            remote_url = self._git("remote", "get-url", remote)
            remote_result = self._git(
                "ls-remote", "--exit-code", "--heads", remote, merge_ref, timeout=15
            )
            latest = remote_result.split()[0]
            if not re.fullmatch(r"[0-9a-fA-F]{40,64}", latest):
                raise RuntimeError("The remote returned an invalid revision")
            dirty = bool(
                self._git("status", "--porcelain", "--untracked-files=normal")
            )
        except (IndexError, RuntimeError):
            return base

        available = current != latest
        repository = _display_repository(remote_url)
        history_blocked = False
        if available:
            try:
                self._git("cat-file", "-e", f"{latest}^{{commit}}")
                self._git("merge-base", "--is-ancestor", current, latest)
            except RuntimeError:
                # A newly published commit is normally absent locally, which is
                # safe to offer. A known commit that is not a descendant means
                # the local branch is ahead or history was rewritten.
                try:
                    self._git("cat-file", "-e", f"{latest}^{{commit}}")
                except RuntimeError:
                    pass
                else:
                    history_blocked = True
        result: dict[str, object] = {
            "supported": True,
            "available": available,
            "can_update": available and not dirty and not history_blocked,
            "state": "available" if available else "current",
            "message": (
                "A software update is available."
                if available
                else "Software is up to date."
            ),
            "repository": repository,
            "remote": remote,
            "branch": merge_ref.removeprefix("refs/heads/"),
            "current_version": current[:12],
            "latest_version": latest[:12],
            "dirty": dirty,
        }
        if available and dirty:
            result["state"] = "blocked"
            result["message"] = (
                "An update is available, but local changes must be handled first."
            )
        elif history_blocked:
            result["state"] = "blocked"
            result["message"] = (
                "The local and remote branches differ; update them manually so no work is lost."
            )
        return result


def _display_repository(remote_url: str) -> str:
    """Return a credential-free host/path label for HTTPS and SSH remotes."""
    scp_match = re.fullmatch(r"[^@\s]+@([^:\s]+):(.+)", remote_url)
    if scp_match:
        host, path = scp_match.groups()
        return f"{host}/{path.removesuffix('.git').strip('/')}"
    parsed = urlsplit(remote_url)
    if parsed.hostname:
        path = parsed.path.removesuffix(".git").strip("/")
        return f"{parsed.hostname}/{path}" if path else parsed.hostname
    return "configured Git remote"
