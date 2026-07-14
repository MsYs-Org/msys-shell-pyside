from __future__ import annotations

import unittest
from unittest import mock

from msys_shell_pyside.status_agent import (
    HAL_INVENTORY_TIMEOUT_SECONDS,
    HAL_MANAGER_INTERFACE,
    HAL_STATE_TIMEOUT_SECONDS,
    battery,
)
from msys_shell_pyside.tk_roles import local_system_status_text, system_status_text
from msys_shell_pyside.localization import SHELL_I18N


class FakeClient:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.calls: list[tuple[str, str, dict, int, bool]] = []

    def call_interface(self, interface, method, payload, timeout=5, *, idempotent=False):
        self.calls.append((interface, method, payload, timeout, idempotent))
        if not self.available:
            return {"type": "error", "code": "NO_PROVIDER"}
        if method == "inventory":
            return {"type": "return", "payload": {"devices": [
                {
                    "id": "power:usb0",
                    "domain": "power",
                    "available": True,
                    "metadata": {"type": "USB"},
                    "provider": "org.example:power",
                },
                {
                    "id": "power:BAT0",
                    "domain": "power",
                    "available": True,
                    "metadata": {"type": "Battery"},
                    "provider": "org.example:power",
                },
            ]}}
        return {"type": "return", "payload": {
            "provider": "org.example:power",
            "state": {
                "id": "power:BAT0",
                "available": True,
                "values": {"capacity_percent": 83, "status": "Discharging"},
            },
        }}


class StatusHalTests(unittest.TestCase):
    def test_battery_uses_hal_only_and_prefers_battery_supply(self) -> None:
        client = FakeClient()
        result = battery(client)
        self.assertEqual(result["capacity"], 83)
        self.assertEqual(result["device"], "power:BAT0")
        self.assertEqual(result["source"], HAL_MANAGER_INTERFACE)
        self.assertEqual([item[1] for item in client.calls], ["inventory", "get_state"])
        self.assertTrue(all(item[0] == HAL_MANAGER_INTERFACE for item in client.calls))
        self.assertTrue(all(item[4] for item in client.calls))
        self.assertEqual(
            [item[3] for item in client.calls],
            [HAL_INVENTORY_TIMEOUT_SECONDS, HAL_STATE_TIMEOUT_SECONDS],
        )
        self.assertGreaterEqual(HAL_INVENTORY_TIMEOUT_SECONDS, 30)
        self.assertLessEqual(HAL_INVENTORY_TIMEOUT_SECONDS, 40)

    def test_missing_hal_is_a_normal_unavailable_snapshot(self) -> None:
        result = battery(FakeClient(available=False))
        self.assertFalse(result["available"])
        self.assertIsNone(result["capacity"])

    def test_chrome_text_shows_typed_hal_capacity(self) -> None:
        self.assertEqual(
            system_status_text({
                "time": "12:34",
                "battery": {"available": True, "capacity": 83},
            }),
            "MSYS  |  12:34  |  BAT 83%",
        )
        self.assertEqual(
            system_status_text({"time": "12:34", "battery": {"available": False}}),
            "MSYS  |  12:34",
        )

    def test_chrome_battery_label_uses_the_selected_shell_locale(self) -> None:
        previous = SHELL_I18N.locale
        try:
            SHELL_I18N.set_locale("zh-CN")
            self.assertEqual(
                system_status_text({
                    "time": "12:34",
                    "battery": {"available": True, "capacity": 83},
                }),
                "MSYS  |  12:34  |  电池 83%",
            )
        finally:
            SHELL_I18N.set_locale(previous)

    def test_chrome_keeps_a_local_clock_without_resident_status_agent(self) -> None:
        with mock.patch(
            "msys_shell_pyside.tk_roles.time.strftime",
            return_value="12:34",
        ):
            self.assertEqual(local_system_status_text(), "MSYS  |  12:34")
            self.assertEqual(
                local_system_status_text({"available": True, "capacity": 83}),
                "MSYS  |  12:34  |  BAT 83%",
            )


if __name__ == "__main__":
    unittest.main()
