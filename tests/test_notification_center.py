from __future__ import annotations

import json
import inspect
import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from msys_shell_pyside.notification_center import (
    HISTORY_SCHEMA,
    INITIAL_RENDER_NOTIFICATIONS,
    MAX_MESSAGE_CHARS,
    NOTIFICATION_TOPICS,
    NotificationCenterService,
    NotificationCenterUi,
    NotificationHistoryStore,
    configure_notification_fonts,
    notification_lines,
    notification_wrap_limit,
    normalize_notification,
    run_tk,
)
from msys_shell_pyside.localization import SHELL_I18N
from msys_shell_pyside.tk_roles import (
    DEFAULT_TOAST_TIMEOUT_MS,
    MAX_TOAST_MESSAGE_CHARS,
    MAX_TOAST_TIMEOUT_MS,
    MIN_TOAST_TIMEOUT_MS,
    bind_system_chrome_notification_toggle,
    notification_message,
    notification_timeout_ms,
)


class NotificationNormalizationTests(unittest.TestCase):
    def test_toast_timeout_is_bounded_and_malformed_input_uses_fallback(self) -> None:
        self.assertEqual(notification_timeout_ms(-1), MIN_TOAST_TIMEOUT_MS)
        self.assertEqual(notification_timeout_ms(10**30), MAX_TOAST_TIMEOUT_MS)
        self.assertEqual(
            notification_timeout_ms("bad", "also-bad"),
            DEFAULT_TOAST_TIMEOUT_MS,
        )
        self.assertEqual(notification_timeout_ms(True, 1800), 1800)
        self.assertEqual(notification_message(None), "")
        self.assertEqual(
            len(notification_message("x" * (MAX_TOAST_MESSAGE_CHARS + 100))),
            MAX_TOAST_MESSAGE_CHARS,
        )

    def test_payload_is_normalized_and_bounded(self) -> None:
        entry = normalize_notification(
            "msys.notification.post",
            {
                "summary": "Build complete",
                "body": "x" * (MAX_MESSAGE_CHARS + 50),
                "application": "org.example.builder",
                "urgency": "high",
            },
            timestamp_ms=1234,
            notification_id="notice-1",
        )
        self.assertEqual(entry["id"], "notice-1")
        self.assertEqual(entry["timestamp_ms"], 1234)
        self.assertEqual(entry["title"], "Build complete")
        self.assertEqual(entry["source"], "org.example.builder")
        self.assertEqual(entry["urgency"], "high")
        self.assertEqual(len(entry["message"]), MAX_MESSAGE_CHARS)
        self.assertTrue(entry["message"].endswith("…"))

    def test_non_mapping_and_title_only_payloads_remain_useful(self) -> None:
        scalar = normalize_notification(
            "msys.role.notification-presenter",
            "hello",
            timestamp_ms=1,
            notification_id="one",
        )
        title_only = normalize_notification(
            "msys.notification.post",
            {"title": "Attention"},
            timestamp_ms=2,
            notification_id="two",
        )
        self.assertEqual(scalar["message"], "hello")
        self.assertEqual(title_only["message"], "Attention")
        self.assertEqual(title_only["title"], "")

    def test_history_copy_wraps_on_narrow_screens_and_is_localized(self) -> None:
        previous = SHELL_I18N.locale
        try:
            SHELL_I18N.set_locale("zh-Hans-CN")
            self.assertEqual(notification_lines([], character_limit=20), ["暂无通知"])
            lines = notification_lines(
                [{
                    "timestamp_ms": 0,
                    "title": "更新",
                    "message": "很长的通知正文" * 12,
                    "source": "org.msys.update",
                }],
                character_limit=18,
            )
            self.assertGreater(len(lines), 2)
            self.assertTrue(all(len(line) <= 18 for line in lines))
            self.assertIn("更新：", "".join(lines))
        finally:
            SHELL_I18N.set_locale(previous)

    def test_pixel_width_maps_to_a_bounded_character_width(self) -> None:
        self.assertEqual(notification_wrap_limit(1), 12)
        self.assertLess(notification_wrap_limit(260), notification_wrap_limit(520))
        self.assertEqual(notification_wrap_limit(100000), 80)


class NotificationHistoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "notifications" / "history.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def item(index: int) -> dict:
        return normalize_notification(
            "msys.notification.post",
            {"message": f"message-{index}"},
            "test",
            timestamp_ms=index,
            notification_id=f"id-{index}",
        )

    def test_history_is_bounded_persistent_and_newest_first(self) -> None:
        store = NotificationHistoryStore(self.path, limit=3)
        for index in range(5):
            store.append(self.item(index))

        self.assertEqual([item["id"] for item in store.list()], ["id-4", "id-3", "id-2"])
        self.assertEqual([item["id"] for item in store.list(2)], ["id-4", "id-3"])
        self.assertEqual(store.list(-1), [])

        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["schema"], HISTORY_SCHEMA)
        self.assertEqual([item["id"] for item in on_disk["notifications"]], ["id-2", "id-3", "id-4"])
        self.assertEqual(
            [item["id"] for item in NotificationHistoryStore(self.path, limit=3).list()],
            ["id-4", "id-3", "id-2"],
        )
        self.assertEqual(list(self.path.parent.glob(f".{self.path.name}.*")), [])

    def test_lower_limit_trims_the_persistent_file_on_open(self) -> None:
        original = NotificationHistoryStore(self.path, limit=5)
        for index in range(5):
            original.append(self.item(index))
        trimmed = NotificationHistoryStore(self.path, limit=2)
        self.assertEqual(trimmed.count(), 2)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual([item["id"] for item in raw["notifications"]], ["id-3", "id-4"])

    def test_corrupt_history_is_replaced_on_first_write(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("not-json", encoding="utf-8")
        store = NotificationHistoryStore(self.path, limit=3)
        self.assertEqual(store.list(), [])
        store.append(self.item(1))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["schema"], HISTORY_SCHEMA)

    def test_clear_is_persistent_and_reports_removed_count(self) -> None:
        store = NotificationHistoryStore(self.path, limit=3)
        store.append(self.item(1))
        store.append(self.item(2))
        self.assertEqual(store.clear(), 2)
        self.assertEqual(store.clear(), 0)
        self.assertEqual(NotificationHistoryStore(self.path, limit=3).list(), [])


class NotificationCenterServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.actions: queue.Queue = queue.Queue()
        self.store = NotificationHistoryStore(
            Path(self.temporary.name) / "history.json",
            limit=3,
        )
        ids = iter(["notification-a", "notification-b"])
        times = iter([1000, 2000])
        self.service = NotificationCenterService(
            self.store,
            self.actions,
            id_factory=lambda: next(ids),
            clock_ms=lambda: next(times),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def call(self, method: str, payload: dict | None = None, request_id: int = 7) -> dict:
        return self.service.handle_call({
            "type": "call",
            "id": request_id,
            "method": method,
            "payload": payload or {},
        })

    def test_subscribed_topics_are_stored_without_opening_panel(self) -> None:
        for topic in sorted(NOTIFICATION_TOPICS):
            self.service.handle_event({
                "type": "event",
                "topic": topic,
                "source": "org.example.sender",
                "payload": {"message": topic},
            })
        ignored = self.service.handle_event({
            "type": "event",
            "topic": "unrelated.topic",
            "payload": {"message": "ignore"},
        })
        self.assertIsNone(ignored)
        self.assertFalse(self.service.visible)
        self.assertEqual(self.store.count(), 2)
        self.assertEqual(self.actions.qsize(), 2)
        self.assertEqual(self.store.list()[0]["source"], "org.example.sender")

    def test_show_hide_and_toggle_publish_ui_visibility(self) -> None:
        shown = self.call("show")
        hidden = self.call("toggle")
        shown_again = self.call("toggle")
        hidden_again = self.call("hide")
        self.assertTrue(shown["payload"]["visible"])
        self.assertFalse(hidden["payload"]["visible"])
        self.assertTrue(shown_again["payload"]["visible"])
        self.assertFalse(hidden_again["payload"]["visible"])
        self.assertEqual(
            [self.actions.get_nowait() for _ in range(4)],
            [
                ("visibility", True),
                ("visibility", False),
                ("visibility", True),
                ("visibility", False),
            ],
        )

    def test_list_and_clear_mipc_methods(self) -> None:
        self.service.handle_event({
            "type": "event",
            "topic": "msys.notification.post",
            "payload": {"message": "first"},
        })
        listed = self.call("list", {"limit": 1})
        self.assertEqual(listed["type"], "return")
        self.assertEqual(listed["payload"]["count"], 1)
        self.assertEqual(listed["payload"]["notifications"][0]["message"], "first")

        cleared = self.call("clear")
        self.assertEqual(cleared["payload"]["removed"], 1)
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(self.actions.get_nowait()[0], "history")
        self.assertEqual(self.actions.get_nowait(), ("history", []))

    def test_bad_limit_and_unknown_method_return_typed_errors(self) -> None:
        bad = self.call("list", {"limit": "many"})
        unknown = self.call("dismiss-one")
        self.assertEqual(bad["code"], "BAD_REQUEST")
        self.assertEqual(unknown["code"], "NO_METHOD")

    def test_ui_model_starts_hidden_without_creating_tk_panel(self) -> None:
        ui = NotificationCenterUi(object(), self.service, lambda: None)
        self.assertFalse(self.service.visible)
        self.assertIsNone(ui.panel)

    def test_320x480_panel_is_light_full_width_material_surface(self) -> None:
        root = SimpleNamespace(
            winfo_screenwidth=lambda: 320,
            winfo_screenheight=lambda: 480,
        )
        ui = NotificationCenterUi(root, self.service, lambda: None)
        rect = ui._panel_rect()
        self.assertEqual((rect.width, rect.height, rect.x, rect.y), (304, 422, 8, 8))

    def test_first_map_renders_a_bounded_prefix_then_finishes_idle(self) -> None:
        class Listbox:
            def __init__(self) -> None:
                self.lines: list[str] = []

            def delete(self, *_args) -> None:
                self.lines.clear()

            def insert(self, _where, line: str) -> None:
                self.lines.append(line)

            def winfo_width(self) -> int:
                return 272

        callbacks: list[object] = []
        root = SimpleNamespace(after_idle=callbacks.append)
        ui = NotificationCenterUi(root, self.service, lambda: None)
        ui.panel = object()
        ui.listbox = Listbox()
        ui.count_label = mock.Mock()
        notifications = [
            normalize_notification(
                "msys.notification.post",
                {"message": f"notice-{index}"},
                timestamp_ms=index,
                notification_id=f"id-{index}",
            )
            for index in range(INITIAL_RENDER_NOTIFICATIONS + 4)
        ]

        ui.refresh(
            notifications,
            maximum=INITIAL_RENDER_NOTIFICATIONS,
            defer_remaining=True,
        )

        self.assertEqual(len(callbacks), 1)
        self.assertEqual(len(ui.listbox.lines), INITIAL_RENDER_NOTIFICATIONS)
        callbacks[0]()
        self.assertEqual(len(ui.listbox.lines), len(notifications))
        ui.count_label.configure.assert_called_with(text=str(len(notifications)))

    def test_mipc_is_ready_before_tk_cold_path(self) -> None:
        source = inspect.getsource(run_tk)
        self.assertLess(source.index("client.ready()"), source.index("NotificationHistoryStore("))
        self.assertLess(source.index("client.ready()"), source.index("tk.Tk("))
        self.assertGreater(source.index('"msys.role.ready"'), source.index("tk.Tk("))
        self.assertIn("root.after(0, pump)", source)


class NotificationFontFastPathTests(unittest.TestCase):
    def test_explicit_family_skips_font_enumeration_policy(self) -> None:
        configured: list[dict[str, object]] = []

        class NamedFont:
            def configure(self, **options) -> None:
                configured.append(options)

        font_module = SimpleNamespace(
            nametofont=lambda _name, **_options: NamedFont(),
        )
        root = SimpleNamespace()
        with (
            mock.patch.dict(
                "os.environ",
                {"MSYS_COMPONENT_ID": "org.msys.shell.pyside:notification-center"},
                clear=True,
            ),
            mock.patch(
                "msys_shell_pyside.notification_center.requested_font_family",
                return_value="Noto Sans CJK SC",
            ),
            mock.patch(
                "msys_shell_pyside.notification_center.configure_tk_fonts"
            ) as full_policy,
            mock.patch(
                "msys_sdk.ui_identity.configure_tk_window_identity"
            ) as identity_policy,
        ):
            selected = configure_notification_fonts(
                root,
                default_size=10,
                font_module=font_module,
            )

        self.assertEqual(selected, "Noto Sans CJK SC")
        self.assertTrue(configured)
        self.assertTrue(all(item["family"] == selected for item in configured))
        self.assertEqual(root._msys_tk_font_family, selected)
        full_policy.assert_not_called()
        identity_policy.assert_called_once_with(
            root,
            "org.msys.shell.notification-center",
        )

    def test_automatic_family_keeps_sdk_cjk_policy(self) -> None:
        root = object()
        with (
            mock.patch(
                "msys_shell_pyside.notification_center.requested_font_family",
                return_value="",
            ),
            mock.patch(
                "msys_shell_pyside.notification_center.configure_tk_fonts",
                return_value="Auto CJK",
            ) as full_policy,
        ):
            selected = configure_notification_fonts(root, default_size=10)
        self.assertEqual(selected, "Auto CJK")
        full_policy.assert_called_once_with(root, default_size=10)


class _ImmediateThread:
    def __init__(self, *, target, **_kwargs) -> None:
        self.target = target

    def start(self) -> None:
        self.target()


class _FakeToplevel:
    def __init__(self) -> None:
        self.bindings: dict[str, object] = {}

    def bind(self, sequence: str, callback, **_kwargs) -> None:
        self.bindings[sequence] = callback

    def winfo_width(self) -> int:
        return 320


class SystemChromeGestureTests(unittest.TestCase):
    @mock.patch("msys_shell_pyside.tk_roles.threading.Thread", _ImmediateThread)
    @mock.patch("msys_shell_pyside.tk_roles.MsysClient.public_call")
    def test_click_and_downward_drag_toggle_once_each(self, public_call) -> None:
        public_call.return_value = {"type": "return", "payload": {"visible": True}}
        root = _FakeToplevel()
        bind_system_chrome_notification_toggle(root)

        with mock.patch("msys_shell_pyside.tk_roles.time.monotonic", side_effect=[1.0, 2.0]):
            root.bindings["<ButtonRelease-1>"](SimpleNamespace(y_root=10))
            root.bindings["<ButtonPress-1>"](SimpleNamespace(y_root=10))
            root.bindings["<B1-Motion>"](SimpleNamespace(y_root=35))
            root.bindings["<ButtonRelease-1>"](SimpleNamespace(y_root=35))

        self.assertEqual(public_call.call_count, 2)
        public_call.assert_called_with(
            "role:notification-center",
            "toggle",
            {},
            timeout=7,
        )

    @mock.patch("msys_shell_pyside.tk_roles.threading.Thread", _ImmediateThread)
    @mock.patch("msys_shell_pyside.tk_roles.MsysClient.public_call")
    def test_right_quick_target_toggles_replaceable_input_method(self, public_call) -> None:
        public_call.return_value = {"type": "return", "payload": {"visible": True}}
        root = _FakeToplevel()
        bind_system_chrome_notification_toggle(root)

        with mock.patch("msys_shell_pyside.tk_roles.time.monotonic", return_value=1.0):
            root.bindings["<ButtonRelease-1>"](
                SimpleNamespace(x=300, y_root=10)
            )

        public_call.assert_called_once_with(
            "role:input-method",
            "toggle",
            {},
            timeout=7,
        )

    @mock.patch("msys_shell_pyside.tk_roles.threading.Thread", _ImmediateThread)
    @mock.patch("msys_shell_pyside.tk_roles.MsysClient.public_call")
    def test_resident_chrome_reuses_its_private_component_channel(self, public_call) -> None:
        private_call = mock.Mock(return_value={"type": "return", "payload": {}})
        root = _FakeToplevel()
        bind_system_chrome_notification_toggle(root, private_call)

        with mock.patch("msys_shell_pyside.tk_roles.time.monotonic", return_value=1.0):
            root.bindings["<ButtonRelease-1>"](SimpleNamespace(x=20, y_root=10))

        private_call.assert_called_once_with(
            "role:notification-center",
            "toggle",
            {},
            timeout=7,
        )
        public_call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
