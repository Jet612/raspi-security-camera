"""Atomic persistence for dashboard-controlled device settings."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path


SETTINGS_VERSION = 3
AI_CATEGORIES = ("person", "vehicle", "animal")


def _validated_integer(
    name: str, value: object, minimum: int, maximum: int
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


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
    capture_width: int = 1920
    capture_height: int = 1080
    capture_fps: int = 30
    capture_quality: int = 85
    live_width: int = 960
    live_height: int = 540
    live_fps: int = 10
    live_quality: int = 55

    @classmethod
    def from_json(
        cls, payload: object, defaults: "DeviceSettings | None" = None
    ) -> "DeviceSettings":
        if (
            not isinstance(payload, dict)
            or isinstance(payload.get("version"), bool)
            or payload.get("version") not in {1, 2, SETTINGS_VERSION}
        ):
            raise ValueError("unsupported or missing settings version")

        expected = {
            "version",
            "camera_enabled",
            "ai_enabled",
            "motion_enabled",
            "motion_sensitivity",
        }
        if payload["version"] >= 2:
            expected.add("ai_categories")
        if payload["version"] == SETTINGS_VERSION:
            expected.update(
                {
                    "capture_width",
                    "capture_height",
                    "capture_fps",
                    "capture_quality",
                    "live_width",
                    "live_height",
                    "live_fps",
                    "live_quality",
                }
            )
        if set(payload) != expected:
            raise ValueError("settings contain missing or unknown fields")

        for name in ("camera_enabled", "ai_enabled", "motion_enabled"):
            if not isinstance(payload[name], bool):
                raise ValueError(f"{name} must be true or false")
        sensitivity = _validated_integer(
            "motion_sensitivity", payload["motion_sensitivity"], 1, 100
        )
        quality_defaults = defaults or cls(True, True, True, 50)
        quality_values = {
            "capture_width": quality_defaults.capture_width,
            "capture_height": quality_defaults.capture_height,
            "capture_fps": quality_defaults.capture_fps,
            "capture_quality": quality_defaults.capture_quality,
            "live_width": quality_defaults.live_width,
            "live_height": quality_defaults.live_height,
            "live_fps": quality_defaults.live_fps,
            "live_quality": quality_defaults.live_quality,
        }
        if payload["version"] == SETTINGS_VERSION:
            ranges = {
                "capture_width": (320, 4608),
                "capture_height": (240, 2592),
                "capture_fps": (1, 60),
                "capture_quality": (1, 100),
                "live_width": (320, 1920),
                "live_height": (240, 1080),
                "live_fps": (1, 30),
                "live_quality": (20, 90),
            }
            quality_values = {
                name: _validated_integer(name, payload[name], *limits)
                for name, limits in ranges.items()
            }

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
            **quality_values,
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
        capture_width: int | None = None,
        capture_height: int | None = None,
        capture_fps: int | None = None,
        capture_quality: int | None = None,
        live_width: int | None = None,
        live_height: int | None = None,
        live_fps: int | None = None,
        live_quality: int | None = None,
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
            quality_updates = {
                "capture_width": (capture_width, 320, 4608),
                "capture_height": (capture_height, 240, 2592),
                "capture_fps": (capture_fps, 1, 60),
                "capture_quality": (capture_quality, 1, 100),
                "live_width": (live_width, 320, 1920),
                "live_height": (live_height, 240, 1080),
                "live_fps": (live_fps, 1, 30),
                "live_quality": (live_quality, 20, 90),
            }
            for name, (value, minimum, maximum) in quality_updates.items():
                if value is not None:
                    changes[name] = _validated_integer(
                        name, value, minimum, maximum
                    )

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
                return DeviceSettings.from_json(json.load(settings_file), defaults)
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
