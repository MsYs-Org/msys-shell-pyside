from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping


PREFERENCES_SCHEMA = "msys.shell-preferences.v1"
PREFERENCES_TOPIC = "msys.shell.preferences.changed"
LAYOUTS = frozenset({"profile", "auto", "mobile", "desktop", "kiosk"})
SORT_ORDERS = frozenset({"name", "component"})
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
PREFERENCE_KEYS = frozenset({
    "layout",
    "wallpaper_color",
    "accent_color",
    "icon_size",
    "show_labels",
    "sort",
})


class PreferenceError(ValueError):
    """A shell preference request is invalid."""


def default_preferences(profile: str | None = None) -> dict[str, Any]:
    return {
        # Persist a semantic link to the product profile, not its current
        # value. Appearance-only changes must not accidentally pin a mobile
        # layout after the device switches to a desktop profile.
        "layout": "profile",
        "wallpaper_color": "#10151c",
        "accent_color": "#66b3ff",
        "icon_size": 56,
        "show_labels": True,
        "sort": "name",
    }


def _colour(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if COLOR.fullmatch(text) is None:
        raise PreferenceError(f"{field} must be #RRGGBB")
    return text.lower()


def normalize_preferences(
    values: Mapping[str, Any],
    *,
    base: Mapping[str, Any] | None = None,
    profile: str | None = None,
    partial: bool = False,
) -> dict[str, Any]:
    """Validate a complete preference document or merge a partial update."""

    if not isinstance(values, Mapping):
        raise PreferenceError("preferences must be an object")
    unknown = sorted(set(values) - PREFERENCE_KEYS)
    if unknown:
        raise PreferenceError(f"unknown preference: {unknown[0]}")
    if partial and not values:
        raise PreferenceError("at least one preference is required")

    result = default_preferences(profile)
    if base is not None:
        # Revalidate the base as untrusted persistent state before applying a
        # request. A corrupt file can never smuggle extra keys into replies.
        result.update(normalize_preferences(base, profile=profile))
    result.update(dict(values))

    layout = str(result.get("layout", "")).strip().lower()
    if layout not in LAYOUTS:
        raise PreferenceError(f"unsupported layout: {layout or '<empty>'}")
    sort_order = str(result.get("sort", "")).strip().lower()
    if sort_order not in SORT_ORDERS:
        raise PreferenceError(f"unsupported sort order: {sort_order or '<empty>'}")
    icon_size = result.get("icon_size")
    if isinstance(icon_size, bool):
        raise PreferenceError("icon_size must be an integer")
    try:
        icon_size = int(icon_size)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PreferenceError("icon_size must be an integer") from exc
    if not 40 <= icon_size <= 96:
        raise PreferenceError("icon_size must be between 40 and 96")
    show_labels = result.get("show_labels")
    if not isinstance(show_labels, bool):
        raise PreferenceError("show_labels must be a boolean")

    return {
        "layout": layout,
        "wallpaper_color": _colour(result.get("wallpaper_color"), "wallpaper_color"),
        "accent_color": _colour(result.get("accent_color"), "accent_color"),
        "icon_size": icon_size,
        "show_labels": show_labels,
        "sort": sort_order,
    }


def preferences_path(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    explicit = str(values.get("MSYS_SHELL_PREFERENCES", "")).strip()
    if explicit:
        return Path(explicit)
    return Path(values.get("MSYS_STATE_DIR", "/opt/msys-state")) / "shell" / "launcher.json"


class PreferenceStore:
    """Small provider-owned JSON store using atomic replacement."""

    def __init__(self, path: Path, *, profile: str | None = None) -> None:
        self.path = Path(path)
        self.profile = profile

    def load_state(self) -> tuple[dict[str, Any], int]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict) or raw.get("schema") != PREFERENCES_SCHEMA:
                raise PreferenceError("unsupported preference document")
            values = raw.get("preferences", {})
            revision = raw.get("revision", 0)
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                raise PreferenceError("revision must be a non-negative integer")
            return normalize_preferences(values, profile=self.profile), revision
        except (OSError, UnicodeError, json.JSONDecodeError, PreferenceError):
            return default_preferences(self.profile), 0

    def load(self) -> dict[str, Any]:
        return self.load_state()[0]

    def save(
        self,
        preferences: Mapping[str, Any],
        *,
        revision: int = 0,
    ) -> dict[str, Any]:
        values = normalize_preferences(preferences, profile=self.profile)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise PreferenceError("revision must be a non-negative integer")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        document = {
            "schema": PREFERENCES_SCHEMA,
            "revision": revision,
            "preferences": values,
        }
        try:
            temporary.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return values


class LauncherPreferenceService:
    """mIPC contract owned by whichever component holds the launcher role."""

    def __init__(
        self,
        store: PreferenceStore,
        *,
        on_change: Callable[[dict[str, Any]], None] | None = None,
        publish: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.store = store
        self.on_change = on_change
        self.publish = publish
        self._lock = threading.RLock()
        self._preferences, self._revision = store.load_state()

    @property
    def preferences(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._preferences)

    def _result(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema": PREFERENCES_SCHEMA,
                "revision": self._revision,
                "preferences": dict(self._preferences),
            }

    def _next_revision(self) -> int:
        return max(self._revision + 1, time.time_ns() // 1_000_000)

    def set_preferences(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            updated = normalize_preferences(
                changes,
                base=self._preferences,
                profile=self.store.profile,
                partial=True,
            )
            self._revision = self._next_revision()
            self._preferences = self.store.save(updated, revision=self._revision)
            snapshot = dict(self._preferences)
            revision = self._revision
        if self.on_change is not None:
            self.on_change(snapshot)
        if self.publish is not None:
            self.publish(
                PREFERENCES_TOPIC,
                {"preferences": snapshot, "revision": revision},
            )
        return self._result()

    def reset_preferences(self) -> dict[str, Any]:
        with self._lock:
            self._revision = self._next_revision()
            self._preferences = self.store.save(
                default_preferences(self.store.profile),
                revision=self._revision,
            )
            snapshot = dict(self._preferences)
            revision = self._revision
        if self.on_change is not None:
            self.on_change(snapshot)
        if self.publish is not None:
            self.publish(
                PREFERENCES_TOPIC,
                {
                    "preferences": snapshot,
                    "revision": revision,
                    "reset": True,
                },
            )
        return self._result()

    def handle_call(self, message: Mapping[str, Any]) -> dict[str, Any]:
        request_id = int(message.get("id", 0))
        method = str(message.get("method", ""))
        payload = message.get("payload", {})
        if not isinstance(payload, Mapping):
            return self._error(request_id, "BAD_REQUEST", "payload must be an object")
        try:
            if method in {"get_preferences", "status"}:
                result = self._result()
            elif method == "set_preferences":
                changes = payload.get("preferences", payload)
                if not isinstance(changes, Mapping):
                    raise PreferenceError("preferences must be an object")
                result = self.set_preferences(changes)
            elif method == "reset_preferences":
                result = self.reset_preferences()
            else:
                return self._error(request_id, "NO_METHOD", method)
        except (OSError, PreferenceError, TypeError, ValueError) as exc:
            return self._error(request_id, "BAD_PREFERENCES", str(exc))
        return {"type": "return", "id": request_id, "payload": result}

    @staticmethod
    def _error(request_id: int, code: str, message: str) -> dict[str, Any]:
        return {
            "type": "error",
            "id": request_id,
            "code": code,
            "message": str(message)[:512],
        }
