from __future__ import annotations

import unittest
from unittest import mock

from msys_shell_pyside import demo_app


class DemoAppFallbackTests(unittest.TestCase):
    def test_missing_pyside_falls_back_to_visible_tk_app(self) -> None:
        with (
            mock.patch.object(demo_app, "run_pyside", side_effect=ImportError("PySide6")),
            mock.patch.object(demo_app, "run_tk", return_value=23) as run_tk,
        ):
            self.assertEqual(demo_app.main(), 23)
        run_tk.assert_called_once_with()

    def test_headless_mode_remains_explicit(self) -> None:
        with (
            mock.patch.dict("os.environ", {"MSYS_SHELL_HEADLESS": "1"}),
            mock.patch.object(demo_app, "run_headless", return_value=7) as headless,
        ):
            self.assertEqual(demo_app.main(), 7)
        headless.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
