from __future__ import annotations

import unittest

from msys_shell_pyside.navigation_gesture import (
    PillGestureStateMachine,
    infer_navigation_edge,
    inward_distance,
)


class PillGestureStateMachineTests(unittest.TestCase):
    def test_short_bottom_swipe_releases_exactly_one_back_action(self) -> None:
        gesture = PillGestureStateMachine()
        gesture.press(160, 22, 10.0, "bottom")
        update = gesture.move(160, 2, 10.12)
        self.assertIsNone(update.action)

        released = gesture.release(160, 2, 10.16, fallback_action="home")

        self.assertEqual(released.action, "close")
        self.assertFalse(released.active)
        self.assertFalse(gesture.active)

    def test_held_swipe_emits_apps_once_before_release(self) -> None:
        gesture = PillGestureStateMachine()
        gesture.press(160, 22, 20.0, "bottom")
        armed = gesture.move(160, -8, 20.10)
        self.assertEqual(armed.phase, "armed")
        self.assertIsNone(armed.action)

        triggered = gesture.hold(20.42)
        duplicate = gesture.move(160, -12, 20.60)
        released = gesture.release(160, -12, 20.70, fallback_action="home")

        self.assertEqual(triggered.action, "apps")
        self.assertEqual(triggered.phase, "triggered")
        self.assertIsNone(duplicate.action)
        self.assertIsNone(released.action)

    def test_distance_reached_after_hold_time_triggers_from_motion(self) -> None:
        gesture = PillGestureStateMachine()
        gesture.press(20, 160, 30.0, "right")
        self.assertIsNone(gesture.hold(30.50).action)

        triggered = gesture.move(-10, 160, 30.51)

        self.assertEqual(triggered.action, "apps")
        self.assertEqual(
            gesture.release(-10, 160, 30.60, fallback_action="apps").action,
            None,
        )

    def test_tap_and_release_only_input_keep_three_zone_fallback(self) -> None:
        gesture = PillGestureStateMachine()
        gesture.press(160, 20, 40.0, "bottom")
        self.assertEqual(
            gesture.release(161, 19, 40.05, fallback_action="home").action,
            "home",
        )
        self.assertEqual(
            gesture.release(300, 20, 40.10, fallback_action="apps").action,
            "apps",
        )

    def test_cancel_never_dispatches_and_next_gesture_starts_cleanly(self) -> None:
        gesture = PillGestureStateMachine()
        gesture.press(160, 22, 50.0, "bottom")
        gesture.move(160, -8, 50.20)

        cancelled = gesture.cancel(50.25)

        self.assertEqual(cancelled.phase, "cancelled")
        self.assertIsNone(cancelled.action)
        self.assertFalse(cancelled.active)
        self.assertIsNone(
            gesture.release(160, -8, 50.30, fallback_action="apps").action
        )
        gesture.press(160, 22, 51.0, "bottom")
        self.assertEqual(
            gesture.release(160, 2, 51.1, fallback_action="home").action,
            "close",
        )

    def test_inward_axis_is_correct_on_all_screen_edges(self) -> None:
        cases = {
            "bottom": (10, 20, 10, -10),
            "top": (10, 2, 10, 32),
            "left": (2, 10, 32, 10),
            "right": (20, 10, -10, 10),
        }
        for edge, points in cases.items():
            with self.subTest(edge=edge):
                self.assertEqual(inward_distance(edge, *points), 30)
                gesture = PillGestureStateMachine()
                gesture.press(points[0], points[1], 60.0, edge)
                gesture.move(points[2], points[3], 60.1)
                self.assertEqual(gesture.hold(60.42).action, "apps")

    def test_edge_inference_handles_rotation_and_both_side_edges(self) -> None:
        self.assertEqual(
            infer_navigation_edge(
                320, 42, root_y=438, screen_width=320, screen_height=480
            ),
            "bottom",
        )
        self.assertEqual(
            infer_navigation_edge(
                320, 42, root_y=0, screen_width=320, screen_height=480
            ),
            "top",
        )
        self.assertEqual(
            infer_navigation_edge(
                42, 320, root_x=438, screen_width=480, screen_height=320
            ),
            "right",
        )
        self.assertEqual(
            infer_navigation_edge(
                42, 320, root_x=0, screen_width=480, screen_height=320
            ),
            "left",
        )
        self.assertEqual(
            infer_navigation_edge(42, 320, preferred="left"),
            "left",
        )


if __name__ == "__main__":
    unittest.main()
