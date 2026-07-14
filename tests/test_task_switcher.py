from __future__ import annotations

from pathlib import Path
import unittest

import msys_shell_pyside.task_switcher as task_switcher
from msys_shell_pyside.task_switcher import (
    activate_recent,
    close_recent,
    panel_motion_frames,
    recent_windows,
    recent_status,
    return_payload,
    task_text,
)


class TaskSwitcherInteractionTests(unittest.TestCase):
    def test_material_cards_have_clear_status_copy(self) -> None:
        self.assertEqual(recent_status({"component": "org.example:main"}).label, "Running")
        self.assertEqual(recent_status({"component": "org.example:main", "active": True}).label, "Active")
        self.assertEqual(recent_status({"component": "org.example:main", "state": "failed"}).tone, "warning")
        self.assertEqual(recent_status({"id": "0x42", "source": "x11"}).label, "External window")
        self.assertEqual(task_text("summary.many", count=3), "3 recent tasks")

    def test_task_copy_uses_the_shared_catalog_and_can_switch_locale(self) -> None:
        translator = task_switcher.TASK_SWITCHER_I18N
        previous = translator.locale
        try:
            translator.set_locale("zh-CN")
            self.assertEqual(task_text("title"), "最近任务")
            self.assertEqual(task_text("summary.many", count=3), "3 个最近任务")
            self.assertEqual(task_text("navigation.back"), "返回")
            self.assertEqual(task_text("navigation.home"), "主页")
            self.assertEqual(task_text("navigation.apps"), "任务")
        finally:
            translator.set_locale(previous)

    def test_open_and_close_motion_is_short_monotonic_and_non_blocking(self) -> None:
        opening = panel_motion_frames(True)
        closing = panel_motion_frames(False)
        self.assertEqual(opening[-1].alpha, 1.0)
        self.assertEqual(opening[-1].offset, 0)
        self.assertGreater(opening[0].offset, opening[-1].offset)
        self.assertLess(opening[0].alpha, opening[-1].alpha)
        self.assertLess(closing[-1].alpha, closing[0].alpha)
        self.assertGreater(closing[-1].offset, closing[0].offset)
        self.assertLessEqual(len(opening), 9)

        source = (
            Path(__file__).resolve().parents[1]
            / "msys_shell_pyside"
            / "task_switcher.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("time.sleep", source)
        self.assertIn("root.after(18", source)

    def test_recents_uses_window_manager_and_normalises_actionable_rows(self) -> None:
        calls = []

        def caller(*args, **kwargs):
            calls.append((*args, kwargs))
            return {
                "type": "return",
                "payload": {
                    "windows": [
                        {
                            "component": "org.example.alpha:main",
                            "identity": "org.example.alpha",
                            "title": "Alpha",
                        },
                        {"id": "0x42", "title": "External", "source": "x11"},
                        {"title": "not actionable"},
                        "invalid",
                    ]
                },
            }

        windows = recent_windows(public_call=caller)

        self.assertEqual(calls, [(
            "role:window-manager",
            "recents",
            {},
            {"timeout": 7},
        )])
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0]["component"], "org.example.alpha:main")
        self.assertEqual(windows[1]["id"], "0x42")

    def test_open_reactivates_exact_managed_component(self) -> None:
        calls = []

        def caller(*args, **kwargs):
            calls.append((*args, kwargs))
            return {
                "type": "return",
                "payload": {
                    "component": "org.example.alpha:main",
                    "state": "ready",
                    "activation": {"ok": True},
                },
            }

        result = activate_recent(
            {"component": "org.example.alpha:main", "title": "Alpha"},
            public_call=caller,
        )

        self.assertEqual(result["state"], "ready")
        self.assertEqual(calls, [(
            "msys.core",
            "start",
            {"component": "org.example.alpha:main"},
            {"timeout": 8},
        )])

    def test_close_activates_selection_then_delegates_to_window_policy(self) -> None:
        calls = []

        def caller(*args, **kwargs):
            calls.append((*args, kwargs))
            if args[:2] == ("msys.core", "start"):
                return {
                    "type": "return",
                    "payload": {
                        "component": "org.example.alpha:main",
                        "state": "ready",
                        "activation": {"ok": True},
                    },
                }
            return {
                "type": "return",
                "payload": {
                    "ok": True,
                    "closed_component": "org.example.alpha:main",
                },
            }

        result = close_recent(
            {"component": "org.example.alpha:main", "title": "Alpha"},
            public_call=caller,
        )

        self.assertEqual(result["closed_component"], "org.example.alpha:main")
        self.assertEqual(
            [(call[0], call[1], call[2]) for call in calls],
            [
                ("msys.core", "start", {"component": "org.example.alpha:main"}),
                ("role:window-manager", "close_active", {}),
            ],
        )

    def test_remote_and_semantic_failures_are_not_reported_as_success(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "NO_PROVIDER"):
            return_payload(
                {"type": "error", "code": "NO_PROVIDER", "message": "missing"},
                "task-switcher.show",
            )
        with self.assertRaisesRegex(RuntimeError, "raise failed"):
            activate_recent(
                {"component": "org.example.alpha:main"},
                public_call=lambda *_args, **_kwargs: {
                    "type": "return",
                    "payload": {
                        "activation_error": {
                            "code": "WINDOW_NOT_FOUND",
                            "message": "raise failed",
                        }
                    },
                },
            )
        with self.assertRaisesRegex(RuntimeError, "no-user-window"):
            close_recent(
                {"component": "org.example.alpha:main"},
                public_call=self._close_rejected,
            )
        with self.assertRaises(ValueError):
            activate_recent({"id": "0x42", "title": "External"})

    @staticmethod
    def _close_rejected(target, method, _payload, **_options):
        if (target, method) == ("msys.core", "start"):
            return {
                "type": "return",
                "payload": {"state": "ready", "activation": {"ok": True}},
            }
        return {
            "type": "return",
            "payload": {"ok": False, "reason": "no-user-window"},
        }

    def test_close_hides_recents_before_close_active_and_restores_on_error(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "msys_shell_pyside"
            / "task_switcher.py"
        ).read_text(encoding="utf-8")
        self.assertIn('if action == "close" and panel is not None:', source)
        self.assertIn("panel.withdraw()", source)
        self.assertIn('caller("role:window-manager", "close_active"', source)
        self.assertIn("panel.deiconify()", source)


if __name__ == "__main__":
    unittest.main()
