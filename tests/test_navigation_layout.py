from __future__ import annotations

import unittest
from unittest import mock

from msys_shell_pyside.tk_roles import (
    NavigationFeedback,
    bind_navigation_button,
    dispatch_navigation_action,
    navigation_action_at,
    navigation_gesture_action,
    navigation_is_vertical,
    navigation_method_unavailable,
    navigation_pill_motion_visual,
    navigation_pill_visual,
    navigation_window_method,
    perform_navigation_action,
    public_return_payload,
    start_background_action,
)


class _Surface:
    def __init__(self) -> None:
        self.bindings = {}
        self.options = {}

    def bind(self, sequence, callback, **_options) -> None:
        self.bindings[sequence] = callback

    def configure(self, **options) -> None:
        self.options.update(options)


class NavigationLayoutTests(unittest.TestCase):
    def test_bottom_bar_uses_horizontal_thirds(self) -> None:
        self.assertFalse(navigation_is_vertical(320, 42))
        self.assertEqual(navigation_action_at(20, 20, 320, 42), "back")
        self.assertEqual(navigation_action_at(160, 20, 320, 42), "home")
        self.assertEqual(navigation_action_at(300, 20, 320, 42), "apps")

    def test_right_bar_uses_vertical_thirds(self) -> None:
        self.assertTrue(navigation_is_vertical(60, 420))
        self.assertEqual(navigation_action_at(30, 20, 60, 420), "back")
        self.assertEqual(navigation_action_at(30, 210, 60, 420), "home")
        self.assertEqual(navigation_action_at(30, 400, 60, 420), "apps")

    def test_coordinates_are_clamped_to_an_action(self) -> None:
        self.assertEqual(navigation_action_at(-10, 0, 320, 42), "back")
        self.assertEqual(navigation_action_at(999, 0, 320, 42), "apps")

    def test_pill_inward_swipe_closes_active_app_in_both_orientations(self) -> None:
        self.assertEqual(
            navigation_gesture_action(160, 32, 160, 8, 320, 42),
            "close",
        )
        self.assertEqual(
            navigation_gesture_action(48, 210, 20, 210, 60, 420),
            "close",
        )
        self.assertEqual(
            navigation_gesture_action(160, 20, 160, 18, 320, 42),
            "home",
        )
        # Release-only input keeps the three-zone fallback usable.
        self.assertEqual(
            navigation_gesture_action(None, None, 300, 20, 320, 42),
            "apps",
        )

    def test_pill_visual_is_small_centered_and_tracks_the_live_edge(self) -> None:
        self.assertEqual(navigation_pill_visual(320, 24), (136, 12, 184, 12, 4))
        self.assertEqual(navigation_pill_visual(24, 320), (12, 136, 12, 184, 4))
        for width, height in ((1, 1), (8, 3), (3, 8), (320, 24), (24, 320)):
            x1, y1, x2, y2, thickness = navigation_pill_visual(width, height)
            self.assertLess(x1, max(width, 1))
            self.assertLess(x2, max(width, 1))
            self.assertLess(y1, max(height, 1))
            self.assertLess(y2, max(height, 1))
            self.assertGreaterEqual(min(x1, x2, y1, y2), 0)
            self.assertGreaterEqual(thickness, 1)
            self.assertLessEqual(thickness, min(max(width, 1), max(height, 1)))

    def test_pill_motion_tracks_stretches_and_accents_on_each_edge(self) -> None:
        resting = navigation_pill_motion_visual(320, 42, "bottom", 0, 0)
        dragged = navigation_pill_motion_visual(320, 42, "bottom", 28, 1)
        self.assertLess(dragged[1], resting[1])
        self.assertLess(dragged[0], resting[0])
        self.assertGreater(dragged[2], resting[2])
        self.assertGreater(dragged[4], resting[4])
        self.assertNotEqual(dragged[5], resting[5])

        left_rest = navigation_pill_motion_visual(42, 320, "left", 0, 0)
        left_drag = navigation_pill_motion_visual(42, 320, "left", 28, 1)
        right_rest = navigation_pill_motion_visual(42, 320, "right", 0, 0)
        right_drag = navigation_pill_motion_visual(42, 320, "right", 28, 1)
        self.assertGreater(left_drag[0], left_rest[0])
        self.assertLess(right_drag[0], right_rest[0])

        for edge, size in (("top", (320, 42)), ("bottom", (320, 42)),
                           ("left", (42, 320)), ("right", (42, 320))):
            visual = navigation_pill_motion_visual(*size, edge, 999, 1)
            x1, y1, x2, y2 = visual[:4]
            self.assertTrue(0 <= x1 < size[0])
            self.assertTrue(0 <= x2 < size[0])
            self.assertTrue(0 <= y1 < size[1])
            self.assertTrue(0 <= y2 < size[1])

    def test_compatibility_gesture_helper_accepts_left_edge_policy(self) -> None:
        self.assertEqual(
            navigation_gesture_action(
                8, 210, 30, 210, 42, 420, edge="left"
            ),
            "close",
        )

    def test_back_home_and_pill_close_keep_distinct_policy_semantics(self) -> None:
        self.assertEqual(navigation_window_method("back"), "back")
        self.assertEqual(navigation_window_method("home"), "home")
        self.assertEqual(navigation_window_method("close"), "close_active")
        with self.assertRaises(ValueError):
            navigation_window_method("power")

    def test_concrete_button_binding_stops_toplevel_redispatch(self) -> None:
        surface = _Surface()
        actions = []
        bind_navigation_button(surface, lambda: actions.append("back"))

        self.assertEqual(
            surface.bindings["<ButtonPress-1>"](object()),
            "break",
        )
        self.assertEqual(surface.options["relief"], "sunken")
        self.assertEqual(
            surface.bindings["<ButtonRelease-1>"](object()),
            "break",
        )
        self.assertEqual(actions, ["back"])
        self.assertEqual(surface.options["relief"], "raised")

    def test_broker_action_is_started_without_running_on_the_tk_caller(self) -> None:
        called = []
        thread = mock.Mock()
        with mock.patch(
            "msys_shell_pyside.tk_roles.threading.Thread",
            return_value=thread,
        ) as constructor:
            result = start_background_action("navigation-back", lambda: called.append(1))

        self.assertIs(result, thread)
        self.assertEqual(called, [])
        thread.start.assert_called_once_with()
        self.assertTrue(constructor.call_args.kwargs["daemon"])

    def test_navigation_treats_remote_error_packets_as_failures(self) -> None:
        self.assertEqual(
            public_return_payload(
                {"type": "return", "payload": {"ok": True}},
                "window-manager.home",
            ),
            {"ok": True},
        )
        with self.assertRaisesRegex(RuntimeError, "NO_PROVIDER"):
            public_return_payload(
                {
                    "type": "error",
                    "code": "NO_PROVIDER",
                    "message": "task switcher is unavailable",
                },
                "task-switcher.show",
            )

    def test_typed_navigation_is_the_primary_path_for_buttons(self) -> None:
        calls = []

        def caller(target, method, payload, timeout):
            calls.append((target, method, payload, timeout))
            return {
                "type": "return",
                "payload": {
                    "ok": True,
                    "schema": "msys.navigation-action.v1",
                    "action": payload["action"],
                    "input": payload["input"],
                },
            }

        result = dispatch_navigation_action("home", public_call=caller)

        self.assertFalse(result.legacy)
        self.assertEqual(result.method, "window-manager.navigation_action")
        self.assertEqual(
            calls,
            [("role:window-manager", "navigation_action", {
                "action": "home",
                "input": "button",
            }, 7)],
        )

    def test_held_recents_dispatch_is_typed_apps_with_swipe_input(self) -> None:
        calls = []

        def caller(target, method, payload, timeout):
            calls.append((target, method, payload, timeout))
            return {
                "type": "return",
                "payload": {
                    "ok": True,
                    "schema": "msys.navigation-action.v1",
                    "action": payload["action"],
                    "input": payload["input"],
                },
            }

        result = dispatch_navigation_action(
            "apps",
            "swipe",
            public_call=caller,
        )

        self.assertEqual(result.method, "window-manager.navigation_action")
        self.assertEqual(calls, [(
            "role:window-manager",
            "navigation_action",
            {"action": "apps", "input": "swipe"},
            7,
        )])

    def test_pill_inward_swipe_uses_typed_back_and_legacy_close_active(self) -> None:
        typed_calls = []

        def typed_caller(target, method, payload, timeout):
            typed_calls.append((target, method, payload, timeout))
            return {
                "type": "return",
                "payload": {
                    "ok": True,
                    "schema": "msys.navigation-action.v1",
                    "action": "back",
                    "input": "swipe",
                },
            }

        dispatch_navigation_action("close", public_call=typed_caller)
        self.assertEqual(typed_calls[0][2], {"action": "back", "input": "swipe"})

        legacy_calls = []

        def legacy_caller(target, method, payload, timeout):
            legacy_calls.append((target, method, payload, timeout))
            if method in {"navigation_action", "navigate"}:
                return {"type": "error", "code": "NO_METHOD", "message": method}
            return {"type": "return", "payload": {"ok": True}}

        result = dispatch_navigation_action("close", public_call=legacy_caller)
        self.assertTrue(result.legacy)
        self.assertEqual(result.method, "window-manager.close_active")
        self.assertEqual(
            [call[1] for call in legacy_calls],
            ["navigation_action", "navigate", "close_active"],
        )

    def test_navigate_alias_precedes_legacy_methods(self) -> None:
        calls = []

        def caller(target, method, payload, timeout):
            calls.append(method)
            if method == "navigation_action":
                return {"type": "error", "code": "NO_METHOD", "message": method}
            return {
                "type": "return",
                "payload": {"ok": True, "schema": "msys.navigation-action.v1"},
            }

        result = dispatch_navigation_action("apps", public_call=caller)
        self.assertEqual(result.method, "window-manager.navigate")
        self.assertEqual(calls, ["navigation_action", "navigate"])

    def test_semantic_failure_never_replays_through_a_fallback(self) -> None:
        calls = []

        def caller(target, method, payload, timeout):
            calls.append(method)
            return {
                "type": "return",
                "payload": {
                    "ok": False,
                    "schema": "msys.navigation-action.v1",
                    "reason": "home-visible",
                },
            }

        with self.assertRaisesRegex(RuntimeError, "home-visible"):
            dispatch_navigation_action("back", public_call=caller)
        self.assertEqual(calls, ["navigation_action"])

    def test_old_unknown_method_payload_is_recognised_for_compatibility(self) -> None:
        response = {
            "type": "return",
            "payload": {
                "ok": False,
                "error": "unknown method navigation_action",
            },
        }
        self.assertTrue(navigation_method_unavailable(response, "navigation_action"))
        response["payload"]["schema"] = "msys.navigation-action.v1"
        self.assertFalse(navigation_method_unavailable(response, "navigation_action"))

    def test_legacy_apps_keeps_recents_fallback_but_marks_it_visible(self) -> None:
        calls = []

        def caller(target, method, payload, timeout):
            calls.append((target, method))
            if method in {"navigation_action", "navigate"}:
                return {"type": "error", "code": "NO_METHOD", "message": method}
            if target == "role:task-switcher":
                return {"type": "error", "code": "NO_PROVIDER", "message": "missing"}
            return {
                "type": "return",
                "payload": {"windows": [{"title": "Editor"}]},
            }

        result = dispatch_navigation_action("apps", public_call=caller)
        self.assertTrue(result.legacy)
        self.assertEqual(result.method, "window-manager.recents")
        self.assertIn("1 recent task", result.warning)
        self.assertEqual(calls[-2:], [
            ("role:task-switcher", "show"),
            ("role:window-manager", "recents"),
        ])

    def test_failed_action_reports_local_feedback_without_broadcasting(self) -> None:
        feedback = []
        calls = []

        def caller(target, method, payload, timeout):
            calls.append((target, method))
            return {
                "type": "return",
                "payload": {
                    "ok": False,
                    "schema": "msys.navigation-action.v1",
                    "reason": "no-user-window",
                },
            }

        result = perform_navigation_action(
            "back",
            public_call=caller,
            feedback=feedback.append,
        )
        self.assertIsNone(result)
        self.assertEqual(len(feedback), 1)
        self.assertIsInstance(feedback[0], NavigationFeedback)
        self.assertFalse(feedback[0].ok)
        self.assertIn("no-user-window", feedback[0].detail)
        self.assertEqual(calls, [("role:window-manager", "navigation_action")])


if __name__ == "__main__":
    unittest.main()
