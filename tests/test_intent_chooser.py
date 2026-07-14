from __future__ import annotations

import json
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path

from msys_shell_pyside.intent_chooser import (
    IntentCandidate,
    IntentChooserService,
    IntentPreferenceStore,
    PendingChoice,
    countdown_text,
    normalize_candidates,
    preference_key,
)


class IntentScopeTests(unittest.TestCase):
    def test_countdown_uses_shared_locale_and_preformatted_number(self) -> None:
        from msys_shell_pyside.localization import SHELL_I18N

        previous = SHELL_I18N.locale
        try:
            SHELL_I18N.set_locale("en-US")
            self.assertEqual(countdown_text(1.26), "1.3s")
            self.assertEqual(countdown_text(-2), "0.0s")
            SHELL_I18N.set_locale("zh-CN")
            self.assertEqual(countdown_text(1.26), "1.3 秒")
        finally:
            SHELL_I18N.set_locale(previous)

    def test_uri_preference_is_scoped_to_scheme_not_resource(self) -> None:
        first = preference_key({"action": "open-uri", "uri": "DEMO://one/item"})
        second = preference_key({"action": "open-uri", "uri": "demo://two/elsewhere"})
        other = preference_key({"action": "open-uri", "uri": "https://example.test"})
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)

    def test_mime_and_settings_have_independent_scopes(self) -> None:
        text = preference_key({"action": "open-mime", "mime": "Text/Plain"})
        image = preference_key({"action": "open-mime", "mime": "image/png"})
        display = preference_key({"action": "settings-panel", "name": "display"})
        network = preference_key({"action": "settings-panel", "name": "network"})
        self.assertNotEqual(text, image)
        self.assertNotEqual(display, network)
        self.assertIn("text/plain", text)

    def test_candidate_normalization_rejects_invalid_and_duplicates(self) -> None:
        candidates = normalize_candidates([
            {"component": "org.example:first", "name": "First", "priority": "10"},
            {"component": "org.example:first", "name": "Duplicate"},
            {"component": "", "name": "Missing"},
            "not-an-object",
            {"component": "org.example:second", "priority": "invalid"},
        ])
        self.assertEqual([item.component for item in candidates], [
            "org.example:first",
            "org.example:second",
        ])
        self.assertEqual(candidates[0].priority, 10)
        self.assertEqual(candidates[1].priority, 0)


class IntentPreferenceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "preferences" / "intents.json"
        self.store = IntentPreferenceStore(self.path)
        self.request = {"action": "open-uri", "uri": "demo://hello"}
        self.first = IntentCandidate("org.example:first", "First", "native")
        self.second = IntentCandidate("org.example:second", "Second", "python")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_remember_and_resolve(self) -> None:
        self.assertTrue(self.store.remember(self.request, self.second.component))
        selected = self.store.resolve(self.request, [self.first, self.second])
        self.assertEqual(selected, self.second)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["schema"], "msys.intent-preferences.v1")

    def test_stale_handler_is_discarded(self) -> None:
        self.store.remember(self.request, self.second.component)
        self.assertIsNone(self.store.resolve(self.request, [self.first]))
        self.assertEqual(self.store.list_preferences(), {})

    def test_corrupt_or_unknown_file_is_ignored(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("not-json", encoding="utf-8")
        self.assertIsNone(self.store.resolve(self.request, [self.first]))
        self.path.write_text(
            json.dumps({"schema": "future.schema", "preferences": {"x": "y"}}),
            encoding="utf-8",
        )
        self.assertEqual(self.store.list_preferences(), {})

    def test_forget_and_clear_report_changes(self) -> None:
        other = {"action": "open-mime", "mime": "text/plain"}
        self.store.remember(self.request, self.first.component)
        self.store.remember(other, self.second.component)
        self.assertTrue(self.store.forget(self.request))
        self.assertFalse(self.store.forget(self.request))
        self.assertEqual(self.store.clear(), 1)
        self.assertEqual(self.store.clear(), 0)


class PendingChoiceTests(unittest.TestCase):
    def test_only_first_completion_wins(self) -> None:
        pending = PendingChoice(
            request_id=7,
            request={"action": "open-uri", "uri": "demo://hello"},
            candidates=[IntentCandidate("org.example:first", "First", "native")],
            timeout_ms=4500,
        )
        first = {"type": "return", "id": 7, "payload": {"component": "org.example:first"}}
        second = {"type": "error", "id": 7, "code": "CHOICE_TIMEOUT"}
        self.assertTrue(pending.complete(first))
        self.assertFalse(pending.complete(second))
        self.assertIs(pending.response, first)
        self.assertTrue(pending.event.is_set())


class IntentChooserServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = IntentPreferenceStore(Path(self.temporary.name) / "intents.json")
        self.actions: queue.Queue = queue.Queue()
        self.service = IntentChooserService(self.store, self.actions, timeout_ms=1000)
        self.request = {"action": "open-uri", "uri": "demo://hello"}
        self.candidates = [
            {"component": "org.example:first", "name": "First", "runtime": "native"},
            {"component": "org.example:second", "name": "Second", "runtime": "python"},
        ]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def call(self, candidates: list[dict] | None = None, deadline_ms: int | None = None) -> dict:
        message = {
            "type": "call",
            "id": 9,
            "method": "choose_intent",
            "payload": {
                "request": self.request,
                "candidates": self.candidates if candidates is None else candidates,
            },
        }
        if deadline_ms is not None:
            message["deadline_ms"] = deadline_ms
        return self.service.handle_call(message)

    def test_single_candidate_returns_without_opening_ui(self) -> None:
        response = self.call([self.candidates[0]])
        self.assertEqual(response["payload"]["component"], "org.example:first")
        self.assertTrue(self.actions.empty())

    def test_remembered_candidate_returns_without_opening_ui(self) -> None:
        self.store.remember(self.request, "org.example:second")
        response = self.call()
        self.assertEqual(response["payload"]["component"], "org.example:second")
        self.assertTrue(response["payload"]["remembered"])
        self.assertTrue(self.actions.empty())

    def test_ambiguous_call_waits_for_graphical_selection(self) -> None:
        result: list[dict] = []
        worker = threading.Thread(target=lambda: result.append(self.call()))
        worker.start()
        action, pending = self.actions.get(timeout=0.5)
        self.assertEqual(action, "show")
        self.assertEqual([item.component for item in pending.candidates], [
            "org.example:first",
            "org.example:second",
        ])
        pending.complete({
            "type": "return",
            "id": 9,
            "payload": {"component": "org.example:second", "remembered": False},
        })
        worker.join(timeout=0.5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0]["payload"]["component"], "org.example:second")

    def test_graphical_wait_is_bounded_by_forwarded_caller_deadline(self) -> None:
        result: list[dict] = []
        deadline = int(time.monotonic() * 1000 + 2000)
        worker = threading.Thread(target=lambda: result.append(self.call(deadline_ms=deadline)))
        worker.start()
        action, pending = self.actions.get(timeout=0.5)
        self.assertEqual(action, "show")
        self.assertGreaterEqual(pending.timeout_ms, 450)
        self.assertLessEqual(pending.timeout_ms, 550)
        pending.complete({
            "type": "return",
            "id": 9,
            "payload": {"component": "org.example:first", "remembered": False},
        })
        worker.join(timeout=0.5)
        self.assertFalse(worker.is_alive())

    def test_cancel_choice_unblocks_pending_request_for_back_navigation(self) -> None:
        result: list[dict] = []
        worker = threading.Thread(target=lambda: result.append(self.call()))
        worker.start()
        action, pending = self.actions.get(timeout=0.5)
        self.assertEqual(action, "show")

        cancelled = self.service.handle_call({
            "type": "call",
            "id": 10,
            "method": "cancel_choice",
            "payload": {},
        })
        self.assertTrue(cancelled["payload"]["cancelled"])
        worker.join(timeout=0.5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0]["code"], "CHOICE_CANCELLED")
        dismiss, dismissed_pending = self.actions.get(timeout=0.5)
        self.assertEqual(dismiss, "dismiss")
        self.assertIs(dismissed_pending, pending)

    def test_bad_request_and_unknown_method_return_typed_errors(self) -> None:
        bad = self.service.handle_call({"id": 1, "method": "choose_intent", "payload": {}})
        unknown = self.service.handle_call({"id": 2, "method": "unknown", "payload": {}})
        self.assertEqual(bad["code"], "BAD_CHOICE_REQUEST")
        self.assertEqual(unknown["code"], "NO_METHOD")


if __name__ == "__main__":
    unittest.main()
