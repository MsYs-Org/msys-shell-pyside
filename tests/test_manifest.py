from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

from msys_shell_pyside import __version__


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "manifest.json"
DISPLAY_PROVIDERS = {
    "org.msys.openstick.ch347:x11-spi-touch-output",
    "org.msys.x11.session:hdmi-output",
}


class ShellManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(MANIFEST.read_text(encoding="utf-8-sig"))
        cls.components = {
            component["id"]: component for component in cls.document["components"]
        }

    def test_manifest_is_the_canonical_shell_package(self) -> None:
        self.assertEqual(self.document["schema"], "msys.manifest.v1")
        self.assertEqual(self.document["package"]["id"], "org.msys.shell.pyside")
        self.assertEqual(self.document["package"]["version"], __version__)
        ids = [component["id"] for component in self.document["components"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("launcher", self.components)
        self.assertIn("transitions", self.components)
        self.assertNotIn("window-policy", self.components)

    def test_package_presentation_uses_the_shared_i18n_catalog(self) -> None:
        metadata = self.document["package"]["x-msys-i18n"]
        self.assertEqual(
            metadata,
            {
                "catalog": "files/share/i18n/catalog.json",
                "name_key": "package.name",
                "summary_key": "package.summary",
            },
        )
        catalog = json.loads(
            (ROOT / metadata["catalog"]).read_text(encoding="utf-8")
        )["messages"]
        for locale in ("en-US", "zh"):
            self.assertIn(metadata["name_key"], catalog[locale])
            self.assertIn(metadata["summary_key"], catalog[locale])

    def test_transition_presenter_is_a_ready_replaceable_visual_role(self) -> None:
        transition = self.components["transitions"]
        self.assertEqual(
            transition["exec"],
            ["python", "-m", "msys_shell_pyside.transition_presenter"],
        )
        self.assertEqual(transition["lifecycle"], "on-demand")
        self.assertEqual(transition["idle_timeout_ms"], 15000)
        self.assertEqual(transition["restart"], "on-failure")
        self.assertEqual(
            transition["readiness"], {"mode": "mipc-ready", "timeout_ms": 5000}
        )
        self.assertEqual(set(transition["after"]), DISPLAY_PROVIDERS)
        self.assertEqual(transition["windowing"]["mode"], "overlay")
        self.assertEqual(
            transition["windowing"]["identity"]["x11_wm_class"],
            "org.msys.shell.transitions",
        )
        self.assertEqual(transition["provides"], [{
            "role": "transition-presenter",
            "exclusive": True,
            "priority": 50,
        }])
        self.assertIsNotNone(importlib.util.find_spec("msys_shell_pyside.transition_presenter"))

    def test_screen_shield_is_a_typed_on_demand_provider(self) -> None:
        shield = self.components["screen-shield"]
        self.assertEqual(
            shield["exec"],
            ["python", "-m", "msys_shell_pyside.screen_shield"],
        )
        self.assertEqual(shield["lifecycle"], "manual")
        self.assertEqual(
            shield["env"]["MSYS_SCREEN_SHIELD_TOUCH_DISMISS"],
            "1",
        )
        self.assertIn(
            "mipc.event:subscribe:msys.role.screen-shield",
            shield["permissions"],
        )
        self.assertEqual(shield["provides"], [{
            "role": "screen-shield",
            "exclusive": True,
            "priority": 50,
        }])
        self.assertIsNotNone(importlib.util.find_spec("msys_shell_pyside.screen_shield"))

    def test_visual_roles_follow_the_selected_display_session(self) -> None:
        for component_id in (
            "launcher",
            "transitions",
            "chrome",
            "notifications",
            "notification-center",
            "task-switcher",
            "intent-chooser",
            "screen-shield",
            "navigation",
            "navigation-pill",
        ):
            with self.subTest(component=component_id):
                component = self.components[component_id]
                self.assertEqual(set(component["after"]), DISPLAY_PROVIDERS)
                self.assertNotIn("DISPLAY", component.get("env", {}))
                self.assertEqual(component["windowing"]["display"], "inherit")
                self.assertIn("display:x11", component.get("permissions", []))
                self.assertNotIn(
                    "org.msys.openstick.ch347:x11-spi-touch-output",
                    component.get("requires", []),
                )
                self.assertEqual(component["readiness"]["mode"], "mipc-ready")

    def test_role_permissions_describe_actual_ipc_and_state_boundaries(self) -> None:
        expected = {
            "launcher": {
                "mipc.call:msys.core",
                "mipc.event:subscribe:msys.layout.changed",
                "state:shell-preferences:read-write",
            },
            "chrome": {
                "mipc.call:role:notification-center",
                "mipc.call:role:input-method",
            },
            "status-agent": {
                "mipc.call:org.msys.hal.manager.v1",
                "mipc.event:publish:msys.status.tick",
            },
            "notification-center": {
                "mipc.event:subscribe:msys.notification.post",
                "state:notifications:read-write",
            },
            "task-switcher": {
                "mipc.call:msys.core",
                "mipc.call:role:window-manager",
            },
            "intent-chooser": {"state:intent-preferences:read-write"},
            "navigation": {
                "mipc.call:role:task-switcher",
                "mipc.call:role:window-manager",
            },
            "navigation-pill": {
                "mipc.call:role:task-switcher",
                "mipc.call:role:window-manager",
            },
        }
        for component_id, required in expected.items():
            with self.subTest(component=component_id):
                component = self.components[component_id]
                self.assertTrue(required.issubset(set(component.get("permissions", []))))

        for component_id in ("navigation", "navigation-pill"):
            self.assertNotIn(
                "mipc.call:msys.core",
                self.components[component_id]["permissions"],
            )

    def test_lean_shell_keeps_optional_roles_declared_but_not_resident(self) -> None:
        self.assertEqual(
            {
                component_id
                for component_id, component in self.components.items()
                if component["lifecycle"] == "background"
            },
            {"launcher", "chrome", "navigation", "navigation-pill"},
        )
        idle_roles = {
            "transitions": ("transition-presenter", 15000),
            "notifications": ("notification-presenter", 15000),
            "notification-center": ("notification-center", 60000),
            "task-switcher": ("task-switcher", 60000),
            "intent-chooser": ("chooser", 30000),
        }
        for component_id, (role, timeout_ms) in idle_roles.items():
            with self.subTest(component=component_id):
                component = self.components[component_id]
                self.assertEqual(component["lifecycle"], "on-demand")
                self.assertEqual(component["idle_timeout_ms"], timeout_ms)
                self.assertTrue(any(
                    provide.get("role") == role
                    for provide in component["provides"]
                ))
        status = self.components["status-agent"]
        self.assertEqual(status["lifecycle"], "on-demand")
        self.assertEqual(status["idle_timeout_ms"], 30000)
        self.assertEqual(status["provides"][0]["capability"], "status.system")
        self.assertEqual(self.components["navigation"]["lifecycle"], "background")

    def test_versioned_role_contract_claims_cover_launcher_and_navigation(self) -> None:
        expected = {
            "launcher": (
                "launcher",
                "org.msys.role.launcher.v1",
                "1.0.0",
            ),
            "navigation": (
                "navigation-bar",
                "org.msys.role.navigation-bar.v1",
                "1.0.0",
            ),
            "navigation-pill": (
                "navigation-bar",
                "org.msys.role.navigation-bar.v1",
                "1.0.0",
            ),
        }
        for component_id, (role, contract_id, version) in expected.items():
            with self.subTest(component=component_id):
                provides = self.components[component_id]["provides"]
                claim = next(item for item in provides if item.get("role") == role)
                self.assertEqual(
                    claim["x-msys-contract"],
                    {"id": contract_id, "version": version},
                )


if __name__ == "__main__":
    unittest.main()
