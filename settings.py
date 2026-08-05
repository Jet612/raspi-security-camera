"""Atomic persistence for dashboard-controlled device settings."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path


SETTINGS_VERSION = 2
AI_CATEGORIES = ("person", "vehicle", "animal")


def _validate_ai_categories(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("ai_categories must contain at least one category")
    if any(not isinstance(item, str) for item in value):
        raise ValueError("ai_categories must contain only strings")
    selected = set(value)
    if len(selected) != len(value) or not selected.issubset(AI_CATEGORIES):
        raise ValueError("ai_categories may contain person, vehicle, or animal")
    return tuple(category for category in AI_CATEGORIES if category in selected)


@dataclass(frozen=True)
class DeviceSettings:
    camera_enabled: bool
    ai_enabled: bool
    motion_enabled: bool
    motion_sensitivity: int
    ai_categories: tuple[str, ...] = AI_CATEGORIES

    @classmethod
    def from_json(cls, payload: object) -> "DeviceSettings":
        if (
            not isinstance(payload, dict)
            or isinstance(payload.get("version"), bool)
            or payload.get("version") not in {1, SETTINGS_VERSION}
        ):
            raise ValueError("unsupported or missing settings version")

        expected = {
            "version",
            "camera_enabled",
            "ai_enabled",
            "motion_enabled",
            "motion_sensitivity",
        }
        if payload["version"] == SETTINGS_VERSION:
            expected.add("ai_categories")
        if set(payload) != expected:
            raise ValueError("settings contain missing or unknown fields")

        for name in ("camera_enabled", "ai_enabled", "motion_enabled"):
            if not isinstance(payload[name], bool):
                raise ValueError(f"{name} must be true or false")
        sensitivity = payload["motion_sensitivity"]
        if isinstance(sensitivity, bool) or not isinstance(sensitivity, int):
            raise ValueError("motion_sensitivity must be an integer")
        if not 1 <= sensitivity <= 100:
            raise ValueError("motion_sensitivity must be between 1 and 100")

        return cls(
            camera_enabled=payload["camera_enabled"],
            ai_enabled=payload["ai_enabled"],
            motion_enabled=payload["motion_enabled"],
            motion_sensitivity=sensitivity,
            ai_categories=(
                AI_CATEGORIES
                if payload["version"] == 1
                else _validate_ai_categories(payload["ai_categories"])
            ),
        )

    def to_json(self) -> dict[str, object]:
        return {"version": SETTINGS_VERSION, **asdict(self)}


class SettingsStore:
    """Keeps a validated in-memory snapshot backed by an atomic JSON file."""

    def __init__(self, path: str | Path, defaults: DeviceSettings) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._settings = self._load(defaults)

    @property
    def current(self) -> DeviceSettings:
        with self._lock:
            return self._settings

    def update(
        self,
        *,
        camera_enabled: bool | None = None,
        ai_enabled: bool | None = None,
        motion_enabled: bool | None = None,
        motion_sensitivity: int | None = None,
        ai_categories: list[str] | tuple[str, ...] | None = None,
    ) -> DeviceSettings:
        with self._lock:
            changes: dict[str, object] = {}
            if camera_enabled is not None:
                if not isinstance(camera_enabled, bool):
                    raise ValueError("camera_enabled must be true or false")
                changes["camera_enabled"] = camera_enabled
            if ai_enabled is not None:
                if not isinstance(ai_enabled, bool):
                    raise ValueError("ai_enabled must be true or false")
                changes["ai_enabled"] = ai_enabled
            if motion_enabled is not None:
                if not isinstance(motion_enabled, bool):
                    raise ValueError("motion_enabled must be true or false")
                changes["motion_enabled"] = motion_enabled
            if motion_sensitivity is not None:
                if isinstance(motion_sensitivity, bool) or not isinstance(
                    motion_sensitivity, int
                ):
                    raise ValueError("motion_sensitivity must be an integer")
                if not 1 <= motion_sensitivity <= 100:
                    raise ValueError("motion_sensitivity must be between 1 and 100")
                changes["motion_sensitivity"] = motion_sensitivity
            if ai_categories is not None:
                changes["ai_categories"] = _validate_ai_categories(ai_categories)

            updated = replace(self._settings, **changes)
            self._write(updated)
            self._settings = updated
            return updated

    def _load(self, defaults: DeviceSettings) -> DeviceSettings:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            file_descriptor = os.open(self.path, flags)
        except FileNotFoundError:
            return defaults
        except OSError as exc:
            raise ValueError(f"cannot open settings file {self.path}: {exc}") from exc

        try:
            metadata = os.fstat(file_descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"settings path is not a regular file: {self.path}")
            if metadata.st_mode & 0o077:
                raise ValueError(
                    "settings file must not be accessible by group or others: "
                    f"{self.path}"
                )
            with os.fdopen(file_descriptor, "r", encoding="utf-8") as settings_file:
                file_descriptor = -1
                return DeviceSettings.from_json(json.load(settings_file))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid settings file {self.path}: {exc}") from exc
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)

    def _write(self, settings: DeviceSettings) -> None:
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        try:
            os.fchmod(file_descriptor, 0o600)
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as settings_file:
                file_descriptor = -1
                json.dump(settings.to_json(), settings_file, separators=(",", ":"))
                settings_file.write("\n")
                settings_file.flush()
                os.fsync(settings_file.fileno())
            os.replace(temporary_name, self.path)
            directory_descriptor = os.open(
                self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
