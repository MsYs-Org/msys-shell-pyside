from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "msys_shell_pyside"


class ShellUiFontIntegrationTests(unittest.TestCase):
    def test_every_ui_entrypoint_uses_the_shared_sdk_policy(self) -> None:
        self.assertFalse((PACKAGE / "ui_fonts.py").exists())
        sources = {
            path.name: path.read_text(encoding="utf-8")
            for path in PACKAGE.glob("*.py")
        }
        tk_entrypoints = {
            name: source for name, source in sources.items() if "tk.Tk(" in source
        }
        self.assertTrue(tk_entrypoints)
        for name, source in tk_entrypoints.items():
            with self.subTest(module=name):
                self.assertEqual(
                    source.count("tk.Tk("),
                    source.count("configure_tk_fonts(root"),
                )
        combined = "\n".join(sources.values())
        self.assertIn("from msys_sdk.ui_fonts import", combined)
        self.assertNotIn("from .ui_fonts import", combined)
        self.assertNotIn('font=("Sans",', combined)
        self.assertNotIn('font=("TkDefaultFont",', combined)
        self.assertEqual(combined.count("QtWidgets.QApplication("), 2)
        self.assertEqual(combined.count("configure_qt_fonts(app,"), 2)

    def test_self_contained_build_vendors_sdk_at_package_root(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("msys-sdk/msys_sdk=msys_sdk", readme)


if __name__ == "__main__":
    unittest.main()
