from __future__ import annotations

import json
import queue
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from msys_shell_pyside.launcher import (
    AppIcon,
    DESKTOP_LAYOUT,
    KIOSK_LAYOUT,
    LauncherTkUi,
    MOBILE_LAYOUT,
    choose_icon,
    blend_colour,
    desktop_grid_columns,
    launcher_layout,
    list_launchable_components,
    load_manifest_presentations,
    manifest_paths_from_env,
    normalize_app,
    photo_scale_factors,
    readable_foreground,
    responsive_icon_size,
    sort_apps,
    start_component,
    stable_placeholder,
)


class _Size:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height

    def winfo_width(self) -> int:
        return self.width

    def winfo_height(self) -> int:
        return self.height


class _Widget:
    def __init__(self, *children: "_Widget") -> None:
        self.children = list(children)
        self.options: dict[str, Any] = {}
        self.bindings: dict[str, Any] = {}

    def configure(self, **options: Any) -> None:
        self.options.update(options)

    def bind(self, event: str, callback: Any) -> None:
        self.bindings[event] = callback

    def winfo_children(self) -> list["_Widget"]:
        return self.children


class LauncherLayoutTests(unittest.TestCase):
    def test_explicit_profile_is_authoritative(self) -> None:
        self.assertEqual(launcher_layout("mobile", 1920, 1080), MOBILE_LAYOUT)
        self.assertEqual(launcher_layout("kiosk", 1920, 1080), KIOSK_LAYOUT)
        self.assertEqual(launcher_layout("desktop", 320, 480), DESKTOP_LAYOUT)

    def test_auto_layout_follows_live_geometry(self) -> None:
        self.assertEqual(launcher_layout("auto", 320, 480), MOBILE_LAYOUT)
        self.assertEqual(launcher_layout("", 800, 480), DESKTOP_LAYOUT)
        self.assertEqual(launcher_layout("future-profile", 720, 1280), DESKTOP_LAYOUT)

    def test_grid_columns_are_bounded_and_responsive(self) -> None:
        self.assertEqual(desktop_grid_columns(80), 1)
        self.assertGreater(desktop_grid_columns(900), desktop_grid_columns(320))
        self.assertEqual(desktop_grid_columns(100000), 12)

    def test_icons_fit_live_mobile_desktop_and_kiosk_geometry(self) -> None:
        self.assertLessEqual(responsive_icon_size(MOBILE_LAYOUT, 96, 220, 160), 40)
        self.assertGreater(
            responsive_icon_size(KIOSK_LAYOUT, 56, 800, 480),
            responsive_icon_size(MOBILE_LAYOUT, 56, 320, 480),
        )
        self.assertLess(
            responsive_icon_size(KIOSK_LAYOUT, 96, 180, 180),
            responsive_icon_size(KIOSK_LAYOUT, 96, 800, 480),
        )
        zoom, subsample = photo_scale_factors(32, 56)
        self.assertGreater(zoom, subsample)
        self.assertLessEqual(32 * zoom // subsample, 56)
        self.assertEqual(photo_scale_factors(512, 56), (1, 10))

    def test_theme_colours_and_sort_order_are_deterministic(self) -> None:
        self.assertEqual(blend_colour("#000000", "#ffffff", 0.5), "#808080")
        self.assertEqual(readable_foreground("#ffffff"), "#111820")
        self.assertEqual(readable_foreground("#000000"), "#ffffff")
        beta = normalize_app({"id": "org.example:beta", "name": "A"})
        alpha = normalize_app({"id": "org.example:alpha", "name": "Z"})
        assert beta is not None and alpha is not None
        self.assertEqual(
            [item.component for item in sort_apps([alpha, beta], "name")],
            ["org.example:beta", "org.example:alpha"],
        )
        self.assertEqual(
            [item.component for item in sort_apps([alpha, beta], "component")],
            ["org.example:alpha", "org.example:beta"],
        )

    def test_live_resize_changes_ui_layout_signature_and_grid(self) -> None:
        ui = LauncherTkUi.__new__(LauncherTkUi)
        ui.profile = "auto"
        ui.root = _Size(320, 480)
        ui.canvas = _Size(300, 420)
        portrait = ui._layout_signature()
        self.assertEqual(portrait[:2], (MOBILE_LAYOUT, 1))

        ui.root.width, ui.root.height = 800, 480
        ui.canvas.width = 780
        landscape = ui._layout_signature()
        self.assertEqual(landscape[0], DESKTOP_LAYOUT)
        self.assertGreater(landscape[1], 1)
        self.assertNotEqual(portrait, landscape)

    def test_profile_preference_follows_live_window_policy_profile(self) -> None:
        ui = LauncherTkUi.__new__(LauncherTkUi)
        ui.profile = "mobile"
        ui.preferences = {
            "layout": "profile",
            "icon_size": 56,
            "show_labels": True,
            "sort": "name",
            "wallpaper_color": "#10151c",
            "accent_color": "#66b3ff",
        }
        ui.root = _Size(800, 480)
        ui.canvas = _Size(780, 420)
        self.assertEqual(ui._layout_signature()[0], MOBILE_LAYOUT)
        ui.profile = "desktop"
        self.assertEqual(ui._layout_signature()[0], DESKTOP_LAYOUT)

    def test_worker_ui_actions_are_pumped_only_by_the_ui_loop(self) -> None:
        callbacks = []

        class Root:
            def after(self, delay, callback):
                callbacks.append((delay, callback))

        ui = LauncherTkUi.__new__(LauncherTkUi)
        ui.root = Root()
        ui.tk = type("Tk", (), {"TclError": RuntimeError})
        ui.ui_actions = queue.SimpleQueue()
        called = []
        ui.post_ui(lambda: called.append("done"))

        ui._pump_ui_actions()

        self.assertEqual(called, ["done"])
        self.assertEqual(callbacks[0][0], 30)


class LauncherPresentationTests(unittest.TestCase):
    def test_placeholder_is_stable_and_component_specific(self) -> None:
        first = stable_placeholder("org.example:first", "Example App")
        repeated = stable_placeholder("org.example:first", "Example App")
        second = stable_placeholder("org.example:second", "Example App")
        self.assertEqual(first, repeated)
        self.assertEqual(first[0], "EA")
        self.assertNotEqual(first[1], second[1])

    def test_normalize_app_reads_forward_compatible_icon_metadata(self) -> None:
        app = normalize_app({
            "id": "org.example:main",
            "name": "Example",
            "runtime": "qt",
            "presentation": {
                "package_root": "/opt/apps/org.example/1.0.0",
                "icons": [
                    {"path": "files/icon-32.png", "size": 32, "mime": "image/png"},
                    {"path": "files/icon-128.png", "size": 128, "mime": "image/png"},
                ],
            },
        })
        assert app is not None
        self.assertEqual(app.component, "org.example:main")
        self.assertEqual(app.runtime, "qt")
        self.assertEqual(len(app.icons), 2)
        self.assertEqual(choose_icon(app.icons, 64).size, 128)
        self.assertTrue(app.icons[0].path.endswith("files/icon-32.png"))

    def test_raster_icon_wins_over_unsupported_vector(self) -> None:
        selected = choose_icon([
            AppIcon("large.svg", 128, "image/svg+xml"),
            AppIcon("small.png", 32, "image/png"),
        ], 64)
        self.assertEqual(selected.path, "small.png")

    def test_installed_manifest_metadata_is_resolved_from_package_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "org.example" / "1.2.3"
            root.mkdir(parents=True)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "schema": "msys.manifest.v1",
                "package": {
                    "id": "org.example",
                    "name": "Example Package",
                    "version": "1.2.3",
                    "kind": "application",
                    "summary": "A manifest-backed application",
                    "icons": [{
                        "path": "files/share/icon.png",
                        "size": 64,
                        "mime": "image/png",
                    }],
                },
                "components": [
                    {
                        "id": "main",
                        "name": "Example Main",
                        "icons": [{
                            "path": "files/share/main.png",
                            "size": 48,
                            "mime": "image/png",
                        }],
                    },
                    {"id": "fallback", "name": "Package Icon"},
                ],
            }), encoding="utf-8")
            catalog = load_manifest_presentations([manifest])
            presentation = catalog["org.example:main"]
            self.assertEqual(presentation["component_name"], "Example Main")
            self.assertEqual(
                presentation["icons"][0]["path"],
                str(root / "files" / "share" / "main.png"),
            )
            self.assertEqual(
                catalog["org.example:fallback"]["icons"][0]["path"],
                str(root / "files" / "share" / "icon.png"),
            )
            app = normalize_app({
                "id": "org.example:main",
                "runtime": "native",
                "_manifest_presentation": presentation,
            })
            assert app is not None
            self.assertEqual(app.name, "Example Main")
            self.assertEqual(app.summary, "A manifest-backed application")

    def test_manifest_presentation_uses_package_local_i18n_without_global_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "org.example" / "1.0.0"
            catalog_path = root / "files" / "share" / "i18n" / "catalog.json"
            catalog_path.parent.mkdir(parents=True)
            catalog_path.write_text(json.dumps({
                "$schema": "https://msys.local/schemas/i18n-catalog.v1.json",
                "schema": "msys.i18n.catalog.v1",
                "id": "org.example",
                "default_locale": "en-US",
                "messages": {
                    "en-US": {"app.name": "Notes", "app.summary": "A note"},
                    "zh-CN": {"app.name": "便笺", "app.summary": "一条便笺"},
                },
            }), encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "schema": "msys.manifest.v1",
                "package": {
                    "id": "org.example",
                    "name": "Example",
                    "x-msys-i18n": {
                        "catalog": "files/share/i18n/catalog.json",
                        "name_key": "app.name",
                        "summary_key": "app.summary",
                    },
                },
                "components": [{
                    "id": "main",
                    "name": "Notes",
                    "summary": "A note",
                }],
            }), encoding="utf-8")
            with mock.patch.dict("os.environ", {"MSYS_LOCALE": "zh-CN"}):
                presentation = load_manifest_presentations([manifest])["org.example:main"]
            app = normalize_app({
                "id": "org.example:main",
                "name": "Core English Name",
                "summary": "Core English summary",
                "_manifest_presentation": presentation,
            })
            assert app is not None
            self.assertEqual(app.name, "便笺")
            self.assertEqual(app.summary, "一条便笺")

    def test_manifest_i18n_catalog_cannot_escape_package_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "package"
            root.mkdir()
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "schema": "msys.manifest.v1",
                "package": {"id": "org.example", "name": "Example"},
                "components": [{
                    "id": "main",
                    "name": "Safe fallback",
                    "x-msys-i18n": {"catalog": "../outside.json", "name_key": "app.name"},
                }],
            }), encoding="utf-8")
            presentation = load_manifest_presentations([manifest])["org.example:main"]
            self.assertEqual(presentation["component_name"], "Safe fallback")

    def test_manifest_paths_include_builtin_and_installed_catalogs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_manifest = root / "config" / "manifests" / "builtin.json"
            config_manifest.parent.mkdir(parents=True)
            config_manifest.write_text("{}", encoding="utf-8")
            installed = root / "installed" / "manifest.json"
            installed.parent.mkdir(parents=True)
            installed.write_text("{}", encoding="utf-8")
            registry = root / "state" / "registry" / "installed.json"
            registry.parent.mkdir(parents=True)
            registry.write_text(
                json.dumps({"packages": [{"path": str(installed.parent)}]}),
                encoding="utf-8",
            )
            paths = manifest_paths_from_env({
                "MSYS_CONFIG_DIR": str(root / "config"),
                "MSYS_STATE_DIR": str(root / "state"),
                "MSYS_PACKAGE_ROOT": str(config_manifest.parent),
            })
            self.assertEqual(set(paths), {config_manifest, installed})


class LauncherRegistryIntegrationTests(unittest.TestCase):
    @staticmethod
    def _write_settings_install(root: Path) -> Path:
        installed = root / "packages" / "org.msys.settings" / "versions" / "0.1.1"
        installed.mkdir(parents=True)
        (installed / "manifest.json").write_text(json.dumps({
            "schema": "msys.manifest.v1",
            "package": {
                "id": "org.msys.settings",
                "name": "MSYS Settings",
                "version": "0.1.1",
                "kind": "application",
                "summary": "Manage the system",
            },
            "components": [{
                "id": "main",
                "name": "Settings",
                "runtime": "tk",
                "exec": ["python", "@package/files/app/main.py"],
                "lifecycle": "manual",
                "restart": "never",
                "activation": {"launchable": True},
            }],
        }), encoding="utf-8")
        registry = root / "registry" / "installed.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps({
            "schema": "msys.install-registry.v1",
            "packages": [{
                "package": "org.msys.settings",
                "version": "0.1.1",
                "path": str(installed),
            }],
        }), encoding="utf-8")
        return installed

    def test_installed_settings_is_a_desktop_app_with_stable_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            self._write_settings_install(state)
            calls: list[tuple[Any, ...]] = []

            def public_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
                calls.append((*args, kwargs))
                return {
                    "type": "return",
                    "payload": {"apps": [{
                        "id": "org.msys.settings:main",
                        "name": "Settings",
                        "runtime": "tk",
                        "launchable": True,
                    }]},
                }

            entries = list_launchable_components(
                env={"MSYS_STATE_DIR": str(state)},
                public_call=public_call,
            )
            self.assertEqual(calls, [("msys.core", "list_apps", {}, {"timeout": 4})])
            self.assertEqual(len(entries), 1)
            app = normalize_app(entries[0])
            assert app is not None
            self.assertEqual(app.component, "org.msys.settings:main")
            self.assertEqual(app.name, "Settings")
            self.assertEqual(app.summary, "Manage the system")
            self.assertEqual(app.icons, ())
            self.assertEqual(app.placeholder_text, "SE")
            self.assertRegex(app.placeholder_color, r"^#[0-9a-f]{6}$")

    def test_registry_metadata_cannot_make_an_app_core_did_not_authorize(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            self._write_settings_install(state)
            entries = list_launchable_components(
                env={"MSYS_STATE_DIR": str(state)},
                public_call=lambda *_args, **_kwargs: {
                    "type": "return", "payload": {"apps": []}
                },
            )
            self.assertEqual(entries, [])

    def test_start_uses_the_exact_component_address(self) -> None:
        calls: list[tuple[Any, ...]] = []

        def public_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append((*args, kwargs))
            return {
                "type": "return",
                "payload": {"component": "org.msys.settings:main", "state": "ready"},
            }

        response = start_component(
            "org.msys.settings:main", public_call=public_call, timeout=9
        )
        self.assertEqual(response["payload"]["state"], "ready")
        self.assertEqual(calls, [(
            "msys.core",
            "start",
            {"component": "org.msys.settings:main"},
            {"timeout": 9},
        )])
        with self.assertRaises(ValueError):
            start_component("", public_call=public_call)
        with self.assertRaises(ValueError):
            start_component("org.example:\x00main", public_call=public_call)

    def test_start_does_not_hide_remote_or_window_activation_failure(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "UNKNOWN_COMPONENT"):
            start_component(
                "org.example:missing",
                public_call=lambda *_args, **_kwargs: {
                    "type": "error",
                    "code": "UNKNOWN_COMPONENT",
                    "message": "not installed",
                },
            )
        with self.assertRaisesRegex(RuntimeError, "WINDOW_NOT_FOUND"):
            start_component(
                "org.example:main",
                public_call=lambda *_args, **_kwargs: {
                    "type": "return",
                    "payload": {
                        "component": "org.example:main",
                        "state": "ready",
                        "activation_error": {
                            "code": "WINDOW_NOT_FOUND",
                            "message": "surface was not mapped",
                        },
                    },
                },
            )

    def test_icon_tile_click_binding_recurses_to_all_children(self) -> None:
        icon = _Widget()
        label = _Widget()
        tile = _Widget(icon, label)
        started: list[str] = []
        ui = LauncherTkUi.__new__(LauncherTkUi)
        ui.start = started.append

        ui._bind_launch(tile, "org.msys.settings:main")

        for widget in (tile, icon, label):
            self.assertEqual(widget.options["cursor"], "hand2")
            widget.bindings["<ButtonRelease-1>"](object())
        self.assertEqual(started, ["org.msys.settings:main"] * 3)


if __name__ == "__main__":
    unittest.main()
