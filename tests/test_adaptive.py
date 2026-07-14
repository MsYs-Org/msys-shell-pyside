from __future__ import annotations

import unittest

from msys_shell_pyside.adaptive import adaptive_panel_rect, edge_bar_rect, full_screen_rect


class AdaptiveGeometryTests(unittest.TestCase):
    def test_mobile_panel_remains_inside_root(self) -> None:
        rect = adaptive_panel_rect(320, 480)
        self.assertGreaterEqual(rect.width, 240)
        self.assertGreaterEqual(rect.height, 220)
        self.assertLessEqual(rect.x + rect.width, 320)
        self.assertLessEqual(rect.y + rect.height, 480)

    def test_desktop_panel_grows_but_is_bounded(self) -> None:
        mobile = adaptive_panel_rect(320, 480)
        desktop = adaptive_panel_rect(1920, 1080)
        self.assertGreater(desktop.width, mobile.width)
        self.assertGreater(desktop.height, mobile.height)
        self.assertLessEqual(desktop.width, 520)
        self.assertLessEqual(desktop.height, 720)

    def test_tiny_root_always_retains_one_pixel(self) -> None:
        rect = adaptive_panel_rect(1, 1)
        self.assertEqual(rect.geometry(), "1x1+0+0")

    def test_edge_bars_follow_live_orientation(self) -> None:
        top = edge_bar_rect(320, 480, "top")
        bottom = edge_bar_rect(320, 480, "bottom")
        right = edge_bar_rect(800, 480, "right")
        self.assertEqual(top.width, 320)
        self.assertEqual(bottom.y + bottom.height, 480)
        self.assertEqual(right.x + right.width, 800)
        self.assertEqual(right.height, 480)

    def test_full_screen_rect_has_no_fixed_resolution(self) -> None:
        self.assertEqual(full_screen_rect(800, 480).geometry(), "800x480+0+0")


if __name__ == "__main__":
    unittest.main()
