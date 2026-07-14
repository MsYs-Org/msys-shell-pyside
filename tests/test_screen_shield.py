from __future__ import annotations

import queue
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from msys_shell_pyside.screen_shield import (
    SCREEN_SHIELD_TOPIC,
    STATUS_SCHEMA,
    ScreenShieldService,
    ScreenShieldTkUi,
    ShieldVisibilityCommand,
    boolean_setting,
    touch_dismiss_from_env,
)


class ScreenShieldServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.actions: queue.Queue[tuple[str, object]] = queue.Queue()
        self.service = ScreenShieldService(self.actions)

    def call(self, method: str, payload: object = None, request_id: int = 7) -> dict:
        return self.service.handle_call({
            "type": "call",
            "id": request_id,
            "method": method,
            "payload": {} if payload is None else payload,
        })

    def test_show_and_hide_are_idempotent_with_explicit_state(self) -> None:
        initial = self.call("status")["payload"]
        self.assertEqual(initial["schema"], STATUS_SCHEMA)
        self.assertFalse(initial["visible"])
        self.assertEqual(initial["revision"], 0)
        self.assertTrue(initial["touch_dismiss_enabled"])

        first_show = self.call("show")["payload"]
        second_show = self.call("show")["payload"]
        self.assertTrue(first_show["visible"])
        self.assertTrue(first_show["changed"])
        self.assertFalse(second_show["changed"])
        self.assertEqual(first_show["revision"], second_show["revision"])
        action, command = self.actions.get_nowait()
        self.assertEqual(action, "visibility")
        self.assertIsInstance(command, ShieldVisibilityCommand)
        self.assertTrue(command.visible)
        self.assertTrue(self.actions.empty())

        first_hide = self.call("hide")["payload"]
        second_hide = self.call("hide")["payload"]
        self.assertFalse(first_hide["visible"])
        self.assertTrue(first_hide["changed"])
        self.assertFalse(second_hide["changed"])
        self.assertEqual(first_hide["revision"], second_hide["revision"])
        self.assertFalse(self.actions.get_nowait()[1].visible)
        self.assertTrue(self.actions.empty())

    def test_toggle_and_compatibility_events_share_one_state_machine(self) -> None:
        toggled = self.call("toggle")["payload"]
        self.assertTrue(toggled["visible"])
        self.assertEqual(toggled["last_reason"], "rpc-toggle")

        handled = self.service.handle_event({
            "type": "event",
            "topic": SCREEN_SHIELD_TOPIC,
            "payload": {"action": "hide"},
        })
        ignored = self.service.handle_event({
            "type": "event",
            "topic": SCREEN_SHIELD_TOPIC,
            "payload": {"action": "blink"},
        })
        self.assertTrue(handled)
        self.assertFalse(ignored)
        self.assertFalse(self.service.visible)
        self.assertEqual(self.service.status()["last_reason"], "event-hide")

        self.assertTrue(self.service.handle_event({
            "topic": SCREEN_SHIELD_TOPIC,
            "payload": {"action": "show"},
        }))
        self.assertTrue(self.service.handle_event({
            "topic": SCREEN_SHIELD_TOPIC,
            "payload": {"action": "toggle"},
        }))
        self.assertFalse(self.service.visible)

    def test_bad_payload_and_unknown_method_return_typed_errors(self) -> None:
        malformed = self.call("show", "not-an-object")
        unknown = self.call("lock-with-password")
        self.assertEqual(malformed["type"], "error")
        self.assertEqual(malformed["code"], "BAD_REQUEST")
        self.assertEqual(unknown["type"], "error")
        self.assertEqual(unknown["code"], "NO_METHOD")

    def test_surface_loss_invalidates_pending_commands_and_can_recover(self) -> None:
        shown = self.service.show()
        stale = self.actions.get_nowait()[1]
        lost = self.service.surface_lost(reason="window-destroyed")
        self.assertTrue(lost["changed"])
        self.assertFalse(lost["visible"])
        self.assertGreater(lost["revision"], stale.revision)
        self.assertTrue(self.actions.empty())

        shown_again = self.service.show()
        replacement = self.actions.get_nowait()[1]
        self.assertTrue(shown_again["visible"])
        self.assertGreater(replacement.revision, stale.revision)
        self.assertNotEqual(shown["revision"], shown_again["revision"])


class ScreenShieldPolicyTests(unittest.TestCase):
    def test_touch_dismiss_defaults_on_and_has_strict_false_values(self) -> None:
        self.assertTrue(touch_dismiss_from_env({}))
        for value in ("0", "false", "NO", "off", "disabled"):
            with self.subTest(value=value):
                self.assertFalse(touch_dismiss_from_env({
                    "MSYS_SCREEN_SHIELD_TOUCH_DISMISS": value,
                }))
        self.assertTrue(boolean_setting("unexpected", default=True))
        self.assertFalse(boolean_setting("unexpected", default=False))

    def test_disabled_touch_is_consumed_without_changing_visibility(self) -> None:
        actions: queue.Queue[tuple[str, object]] = queue.Queue()
        service = ScreenShieldService(actions, touch_dismiss_enabled=False)
        service.show()
        actions.get_nowait()
        ui = ScreenShieldTkUi(_FakeRoot(), service)
        self.assertEqual(ui._touch(SimpleNamespace()), "break")
        self.assertTrue(service.visible)
        self.assertTrue(actions.empty())


class _FakeWidget:
    def __init__(self, *_args, **_kwargs) -> None:
        self.bindings: dict[str, object] = {}
        self.options: dict[str, object] = {}

    def bind(self, sequence: str, callback, **_kwargs) -> None:
        self.bindings[sequence] = callback

    def configure(self, **kwargs) -> None:
        self.options.update(kwargs)

    def pack(self, **_kwargs) -> None:
        pass


class _FakePanel(_FakeWidget):
    instances: list["_FakePanel"] = []

    def __init__(self, *_args, **_kwargs) -> None:
        super().__init__()
        self.exists = True
        self.window_state = "normal"
        self.protocols: dict[str, object] = {}
        self.geometry_value = ""
        self.topmost = False
        self.__class__.instances.append(self)

    def winfo_exists(self) -> bool:
        return self.exists

    def title(self, _value: str) -> None:
        pass

    def attributes(self, name: str, value: object) -> None:
        if name == "-topmost":
            self.topmost = bool(value)

    def resizable(self, *_args) -> None:
        pass

    def protocol(self, name: str, callback) -> None:
        self.protocols[name] = callback

    def withdraw(self) -> None:
        self.window_state = "withdrawn"

    def deiconify(self) -> None:
        self.window_state = "normal"

    def state(self) -> str:
        return self.window_state

    def geometry(self, value: str) -> None:
        self.geometry_value = value

    def update_idletasks(self) -> None:
        pass

    def update(self) -> None:
        pass

    def lift(self) -> None:
        pass

    def destroy(self) -> None:
        self.exists = False
        self.window_state = "destroyed"


class _FakeRoot:
    def __init__(self) -> None:
        self.idle_callbacks: list[object] = []

    def winfo_screenwidth(self) -> int:
        return 800

    def winfo_screenheight(self) -> int:
        return 480

    def after_idle(self, callback) -> None:
        self.idle_callbacks.append(callback)

    def destroy(self) -> None:
        pass


class ScreenShieldUiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakePanel.instances.clear()
        self.fake_tk = SimpleNamespace(
            Toplevel=_FakePanel,
            Label=_FakeWidget,
            TclError=RuntimeError,
        )

    def test_surface_maps_fullscreen_and_touch_drives_service_hide(self) -> None:
        actions: queue.Queue[tuple[str, object]] = queue.Queue()
        service = ScreenShieldService(actions)
        ui = ScreenShieldTkUi(_FakeRoot(), service)
        service.show()
        command = actions.get_nowait()[1]

        with mock.patch.dict(sys.modules, {"tkinter": self.fake_tk}):
            self.assertTrue(ui.apply_visibility(command))
        panel = ui.panel
        label = ui.label
        self.assertIsNotNone(panel)
        self.assertEqual(panel.geometry_value, "800x480+0+0")
        self.assertTrue(panel.topmost)
        self.assertIn("<ButtonRelease-1>", panel.bindings)
        self.assertIn("<ButtonRelease-1>", label.bindings)
        self.assertIn("<Destroy>", panel.bindings)
        self.assertIn("<Unmap>", panel.bindings)

        self.assertEqual(label.bindings["<ButtonRelease-1>"](SimpleNamespace()), "break")
        self.assertFalse(service.visible)
        hide_command = actions.get_nowait()[1]
        self.assertTrue(ui.apply_visibility(hide_command))
        self.assertEqual(panel.window_state, "withdrawn")
        self.assertFalse(panel.topmost)

    def test_unexpected_destroy_recovers_state_and_next_show_recreates(self) -> None:
        actions: queue.Queue[tuple[str, object]] = queue.Queue()
        service = ScreenShieldService(actions)
        ui = ScreenShieldTkUi(_FakeRoot(), service)
        service.show()
        first_command = actions.get_nowait()[1]
        with mock.patch.dict(sys.modules, {"tkinter": self.fake_tk}):
            ui.apply_visibility(first_command)
        first_panel = ui.panel
        self.assertIsNotNone(first_panel)

        first_panel.exists = False
        first_panel.window_state = "destroyed"
        first_panel.bindings["<Destroy>"](SimpleNamespace(widget=first_panel))
        self.assertFalse(service.visible)
        self.assertIsNone(ui.panel)
        destroyed_revision = service.revision

        service.show()
        replacement_command = actions.get_nowait()[1]
        with mock.patch.dict(sys.modules, {"tkinter": self.fake_tk}):
            self.assertTrue(ui.apply_visibility(replacement_command))
        self.assertIsNot(ui.panel, first_panel)
        self.assertGreater(service.revision, destroyed_revision)

    def test_stale_show_is_not_replayed_after_external_surface_loss(self) -> None:
        actions: queue.Queue[tuple[str, object]] = queue.Queue()
        service = ScreenShieldService(actions)
        ui = ScreenShieldTkUi(_FakeRoot(), service)
        service.show()
        stale = actions.get_nowait()[1]
        service.surface_lost(reason="window-unmapped")
        with mock.patch.dict(sys.modules, {"tkinter": self.fake_tk}):
            self.assertFalse(ui.apply_visibility(stale))
        self.assertIsNone(ui.panel)


if __name__ == "__main__":
    unittest.main()
