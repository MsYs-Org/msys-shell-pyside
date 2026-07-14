from __future__ import annotations

import queue
import unittest

from msys_shell_pyside.transition_presenter import (
    HARD_WITHDRAW_GRACE_MS,
    HideCommand,
    MAX_DURATION_MS,
    MIN_DURATION_MS,
    TRANSITION_TOPIC,
    TransitionPresenterService,
    TransitionTkUi,
    TransitionView,
    eased_progress,
    fade_alpha,
    make_transition,
    terminal_matches,
    transition_card_width,
)


class TransitionNormalizationTests(unittest.TestCase):
    def test_aliases_defaults_and_bounds(self) -> None:
        launching = make_transition({
            "phase": "launch",
            "component": "org.example:main",
            "duration_ms": 1,
            "generation": "7",
        }, revision=3)
        closing = make_transition({"phase": "close", "duration_ms": 999999}, revision=4)
        self.assertEqual(launching.phase, "launching")
        self.assertEqual(launching.title, "main")
        self.assertEqual(launching.duration_ms, MIN_DURATION_MS)
        self.assertEqual(launching.generation, 7)
        self.assertEqual(closing.phase, "closing")
        self.assertEqual(closing.duration_ms, MAX_DURATION_MS)
        self.assertLessEqual(MAX_DURATION_MS, 4000)

    def test_bad_phase_and_duration_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_transition({"phase": "spinning"}, 1)
        with self.assertRaises(ValueError):
            make_transition({"phase": "launching", "duration_ms": True}, 1)

    def test_terminal_match_uses_component_and_generation(self) -> None:
        active = TransitionView(1, "launching", "org.example:main", "Main", "main", 500, 4)
        self.assertTrue(terminal_matches(active, {"component": "org.example:main", "generation": 4}))
        self.assertFalse(terminal_matches(active, {"component": "org.other:main", "generation": 4}))
        self.assertFalse(terminal_matches(active, {"generation": 4}))
        self.assertFalse(terminal_matches(active, {"component": "org.example:main", "generation": 5}))

    def test_fade_curve_is_clamped(self) -> None:
        self.assertEqual(fade_alpha(-5, 100, 0.0, 0.9), 0.0)
        self.assertAlmostEqual(fade_alpha(50, 100, 0.0, 0.9), 0.45)
        self.assertEqual(fade_alpha(150, 100, 0.0, 0.9), 0.9)

    def test_card_motion_opens_and_closes_in_opposite_directions(self) -> None:
        self.assertEqual(eased_progress(-1, 8), 0.0)
        self.assertEqual(eased_progress(8, 8), 1.0)
        self.assertGreater(eased_progress(4, 8), 0.5)
        self.assertLess(
            transition_card_width("launching", 0),
            transition_card_width("launching", 1),
        )
        self.assertGreater(
            transition_card_width("closing", 0),
            transition_card_width("closing", 1),
        )


class TransitionPresenterServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.actions: queue.Queue = queue.Queue()
        self.service = TransitionPresenterService(self.actions)

    def event(self, phase: str, component: str = "org.example:main", generation: int = 1) -> bool:
        return self.service.handle_event({
            "type": "event",
            "topic": TRANSITION_TOPIC,
            "payload": {
                "phase": phase,
                "component": component,
                "title": "Example",
                "identity": "org.example.main",
                "generation": generation,
            },
        })

    def test_start_event_shows_and_matching_terminal_event_hides(self) -> None:
        self.assertTrue(self.event("launching"))
        action, view = self.actions.get_nowait()
        self.assertEqual(action, "show")
        self.assertEqual(view.component, "org.example:main")
        self.assertTrue(self.service.status()["visible"])

        self.assertTrue(self.event("launched"))
        action, command = self.actions.get_nowait()
        self.assertEqual(action, "hide")
        self.assertIsInstance(command, HideCommand)
        self.assertEqual(command.phase, "launched")
        self.assertFalse(self.service.status()["visible"])

    def test_closing_and_closed_drive_the_exit_animation_path(self) -> None:
        self.assertTrue(self.event("closing", generation=11))
        action, view = self.actions.get_nowait()
        self.assertEqual(action, "show")
        self.assertEqual(view.phase, "closing")
        self.assertEqual(
            TransitionTkUi._labels(view),
            ("Closing Example", "Returning to the desktop"),
        )

        self.assertTrue(self.event("closed", generation=11))
        action, command = self.actions.get_nowait()
        self.assertEqual(action, "hide")
        self.assertEqual(command.phase, "closed")
        self.assertEqual(command.delay_ms, 60)
        self.assertFalse(self.service.status()["visible"])

    def test_failed_launch_uses_a_readable_but_bounded_exit_delay(self) -> None:
        self.event("launching", generation=3)
        self.actions.get_nowait()
        self.assertTrue(self.event("failed", generation=3))
        action, command = self.actions.get_nowait()
        self.assertEqual(action, "hide")
        self.assertEqual(command.phase, "failed")
        self.assertEqual(command.delay_ms, 320)
        self.assertLessEqual(command.delay_ms, 1000)

    def test_late_terminal_event_does_not_hide_newer_application(self) -> None:
        self.assertTrue(self.event("launching", "org.example:first", 1))
        self.actions.get_nowait()
        self.assertTrue(self.event("launching", "org.example:second", 2))
        self.actions.get_nowait()
        self.assertFalse(self.event("launched", "org.example:first", 1))
        self.assertEqual(self.service.active.component, "org.example:second")
        self.assertTrue(self.actions.empty())

    def test_generation_mismatch_does_not_hide_restarted_component(self) -> None:
        self.event("launching", generation=8)
        self.actions.get_nowait()
        self.assertFalse(self.event("failed", generation=7))
        self.assertTrue(self.service.status()["visible"])

    def test_stale_timeout_cannot_expire_replacement(self) -> None:
        first = self.service.show({"phase": "launching", "component": "one"})
        first_revision = first["transition"]["revision"]
        self.actions.get_nowait()
        second = self.service.show({"phase": "closing", "component": "two"})
        self.actions.get_nowait()
        self.assertFalse(self.service.expire(first_revision))
        self.assertEqual(self.service.active.revision, second["transition"]["revision"])
        self.assertTrue(self.actions.empty())

    def test_current_timeout_auto_hides(self) -> None:
        shown = self.service.show({"phase": "launching", "component": "one"})
        revision = shown["transition"]["revision"]
        self.actions.get_nowait()
        self.assertTrue(self.service.expire(revision))
        action, command = self.actions.get_nowait()
        self.assertEqual(action, "hide")
        self.assertEqual(command.phase, "timeout")
        self.assertFalse(self.service.status()["visible"])

    def test_call_contract_supports_show_hide_status_and_typed_errors(self) -> None:
        shown = self.service.handle_call({
            "type": "call",
            "id": 1,
            "method": "show",
            "payload": {"phase": "closing", "title": "Editor", "duration_ms": 600},
        })
        self.assertEqual(shown["type"], "return")
        self.assertTrue(shown["payload"]["visible"])
        status = self.service.handle_call({"id": 2, "method": "status", "payload": {}})
        self.assertEqual(status["payload"]["transition"]["title"], "Editor")
        hidden = self.service.handle_call({"id": 3, "method": "hide", "payload": {}})
        self.assertFalse(hidden["payload"]["visible"])
        bad = self.service.handle_call({"id": 4, "method": "show", "payload": {"phase": "bad"}})
        unknown = self.service.handle_call({"id": 5, "method": "dance", "payload": {}})
        self.assertEqual(bad["code"], "BAD_REQUEST")
        self.assertEqual(unknown["code"], "NO_METHOD")

    def test_unrelated_or_malformed_event_is_ignored(self) -> None:
        self.assertFalse(self.service.handle_event({"topic": "other", "payload": {}}))
        self.assertFalse(self.service.handle_event({"topic": TRANSITION_TOPIC, "payload": "bad"}))
        self.assertTrue(self.actions.empty())


class TransitionResponsiveGeometryTests(unittest.TestCase):
    class Root:
        def __init__(self, width: int, height: int) -> None:
            self.width = width
            self.height = height

        def winfo_screenwidth(self) -> int:
            return self.width

        def winfo_screenheight(self) -> int:
            return self.height

    def test_mask_geometry_is_derived_each_time_from_live_x11_dimensions(self) -> None:
        root = self.Root(320, 480)
        ui = TransitionTkUi.__new__(TransitionTkUi)
        ui.root = root
        self.assertEqual(ui._screen_geometry(), "320x480+0+0")

        root.width, root.height = 800, 480
        self.assertEqual(ui._screen_geometry(), "800x480+0+0")

    def test_direct_watchdog_withdraws_only_the_owned_revision(self) -> None:
        class Panel:
            def __init__(self) -> None:
                self.withdrawn = 0
                self.attributes_calls = []

            def withdraw(self) -> None:
                self.withdrawn += 1

            def attributes(self, *args) -> None:
                self.attributes_calls.append(args)

        ui = TransitionTkUi.__new__(TransitionTkUi)
        ui.panel = Panel()
        ui._render_revision = 7
        ui._alpha = 0.92
        ui._alpha_supported = False

        self.assertFalse(ui._force_withdraw(6))
        self.assertTrue(ui._force_withdraw(7))
        self.assertEqual(ui.panel.withdrawn, 1)
        self.assertIn(("-topmost", False), ui.panel.attributes_calls)
        self.assertEqual(ui._alpha, 0.0)

    def test_hide_always_schedules_a_queue_independent_hard_deadline(self) -> None:
        class Root:
            def __init__(self) -> None:
                self.delays = []

            def after(self, delay, callback):
                self.delays.append(delay)
                return f"after-{len(self.delays)}"

        ui = TransitionTkUi.__new__(TransitionTkUi)
        ui.root = Root()
        ui.panel = object()
        ui.heading = None
        ui.subtitle = None
        ui._after_ids = set()
        ui._render_revision = 1
        ui._alpha = 0.92
        ui._alpha_supported = False
        ui._cancel_scheduled = lambda: ui._after_ids.clear()

        ui.hide(HideCommand(revision=2, phase="closed", delay_ms=60))

        self.assertIn(60, ui.root.delays)
        self.assertIn(
            60 + ui.FADE_OUT_MS + HARD_WITHDRAW_GRACE_MS,
            ui.root.delays,
        )


if __name__ == "__main__":
    unittest.main()
