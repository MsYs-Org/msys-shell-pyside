from __future__ import annotations

import json
import unittest
from pathlib import Path

from msys_shell_pyside.localization import SHELL_COPY, SHELL_I18N, shell_text


CATALOG = Path(__file__).resolve().parents[1] / "files" / "share" / "i18n" / "catalog.json"


class ShellLocalizationTests(unittest.TestCase):
    def test_catalog_locales_have_the_same_complete_inventory(self) -> None:
        document = json.loads(CATALOG.read_text(encoding="utf-8"))
        messages = document["messages"]
        self.assertEqual(set(messages["en-US"]), set(messages["zh"]))
        self.assertTrue(set(SHELL_COPY).issubset(messages["en-US"]))

    def test_generic_and_scripted_chinese_use_the_base_catalog(self) -> None:
        previous = SHELL_I18N.locale
        try:
            for locale in ("zh", "zh-CN", "zh-Hans-CN", "zh_Hans_CN.UTF-8"):
                SHELL_I18N.set_locale(locale)
                self.assertEqual(shell_text("launcher.refresh"), "刷新")
                self.assertEqual(shell_text("navigation.home"), "主页")
                self.assertEqual(SHELL_I18N.resolved_locale, "zh")
        finally:
            SHELL_I18N.set_locale(previous)

    def test_visible_shell_roles_share_one_selected_locale(self) -> None:
        previous = SHELL_I18N.locale
        try:
            SHELL_I18N.set_locale("zh-CN")
            self.assertEqual(shell_text("launcher.refresh"), "刷新")
            self.assertEqual(shell_text("notification.title"), "通知")
            self.assertEqual(shell_text("chooser.cancel"), "取消")
            self.assertEqual(
                shell_text("chooser.countdown.seconds", seconds="1.3"),
                "1.3 秒",
            )
            self.assertEqual(shell_text("shield.window_title"), "MSYS 屏幕遮罩")
            self.assertEqual(
                shell_text("transition.opening", title="便笺"),
                "正在打开 便笺",
            )
            self.assertEqual(shell_text("package.name"), "MSYS 参考 Shell")
        finally:
            SHELL_I18N.set_locale(previous)


if __name__ == "__main__":
    unittest.main()
