from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from msys_shell_pyside.preferences import (
    PREFERENCES_SCHEMA,
    PREFERENCES_TOPIC,
    LauncherPreferenceService,
    PreferenceError,
    PreferenceStore,
    default_preferences,
    normalize_preferences,
    preferences_path,
)


class PreferenceValidationTests(unittest.TestCase):
    def test_defaults_follow_future_profile_changes_without_pinning_a_layout(self) -> None:
        self.assertEqual(default_preferences("desktop")["layout"], "profile")
        self.assertEqual(default_preferences("future")["layout"], "profile")
        self.assertEqual(
            preferences_path({"MSYS_STATE_DIR": "/state"}),
            Path("/state/shell/launcher.json"),
        )

    def test_partial_updates_are_strict_and_preserve_other_fields(self) -> None:
        current = default_preferences("mobile")
        updated = normalize_preferences(
            {"icon_size": 72, "wallpaper_color": "#ABCDEF"},
            base=current,
            partial=True,
        )
        self.assertEqual(updated["layout"], "profile")
        self.assertEqual(updated["icon_size"], 72)
        self.assertEqual(updated["wallpaper_color"], "#abcdef")
        with self.assertRaises(PreferenceError):
            normalize_preferences({}, base=current, partial=True)
        with self.assertRaises(PreferenceError):
            normalize_preferences({"unknown": True}, base=current, partial=True)
        with self.assertRaises(PreferenceError):
            normalize_preferences({"icon_size": True}, base=current, partial=True)


class PreferenceStoreTests(unittest.TestCase):
    def test_atomic_round_trip_and_corrupt_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "shell" / "launcher.json"
            store = PreferenceStore(path, profile="desktop")
            self.assertEqual(store.load()["layout"], "profile")
            saved = store.save({
                **default_preferences("desktop"),
                "accent_color": "#123456",
                "show_labels": False,
            })
            self.assertEqual(saved["accent_color"], "#123456")
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(document["schema"], PREFERENCES_SCHEMA)
            self.assertEqual(document["revision"], 0)
            self.assertFalse(store.load()["show_labels"])
            path.write_text("not-json", encoding="utf-8")
            self.assertEqual(store.load(), default_preferences("desktop"))


class LauncherPreferenceServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.changes: list[dict] = []
        self.events: list[tuple[str, dict]] = []
        self.service = LauncherPreferenceService(
            PreferenceStore(Path(self.temporary.name) / "launcher.json", profile="mobile"),
            on_change=lambda values: self.changes.append(values),
            publish=lambda topic, payload: self.events.append((topic, payload)),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_get_set_reset_contract_is_persistent_and_broadcasts(self) -> None:
        result = self.service.handle_call({
            "type": "call",
            "id": 4,
            "method": "set_preferences",
            "payload": {
                "preferences": {
                    "layout": "desktop",
                    "icon_size": 80,
                    "show_labels": False,
                }
            },
        })
        self.assertEqual(result["type"], "return")
        self.assertEqual(result["payload"]["preferences"]["icon_size"], 80)
        self.assertGreater(result["payload"]["revision"], 0)
        self.assertEqual(self.events[0][0], PREFERENCES_TOPIC)
        self.assertEqual(
            self.events[0][1]["revision"], result["payload"]["revision"]
        )
        self.assertFalse(self.changes[0]["show_labels"])
        loaded = PreferenceStore(
            Path(self.temporary.name) / "launcher.json", profile="mobile"
        ).load()
        self.assertEqual(loaded["layout"], "desktop")
        restarted = LauncherPreferenceService(
            PreferenceStore(Path(self.temporary.name) / "launcher.json", profile="mobile")
        )
        self.assertEqual(
            restarted.handle_call({
                "type": "call", "id": 11, "method": "get_preferences", "payload": {}
            })["payload"]["revision"],
            result["payload"]["revision"],
        )

        reset = self.service.handle_call({
            "type": "call", "id": 5, "method": "reset_preferences", "payload": {}
        })
        self.assertEqual(reset["payload"]["preferences"]["layout"], "profile")
        self.assertTrue(self.events[-1][1]["reset"])

    def test_bad_requests_return_typed_errors_without_mutating_state(self) -> None:
        before = self.service.preferences
        malformed = self.service.handle_call({
            "type": "call", "id": 8, "method": "set_preferences", "payload": []
        })
        invalid = self.service.handle_call({
            "type": "call",
            "id": 9,
            "method": "set_preferences",
            "payload": {"wallpaper_color": "red"},
        })
        missing = self.service.handle_call({
            "type": "call", "id": 10, "method": "missing", "payload": {}
        })
        self.assertEqual(malformed["code"], "BAD_REQUEST")
        self.assertEqual(invalid["code"], "BAD_PREFERENCES")
        self.assertEqual(missing["code"], "NO_METHOD")
        self.assertEqual(self.service.preferences, before)


if __name__ == "__main__":
    unittest.main()
