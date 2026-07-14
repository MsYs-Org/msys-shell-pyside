from __future__ import annotations

import datetime as _dt
import os
import time
from typing import Any

from msys_sdk import MsysClient


HAL_MANAGER_INTERFACE = "org.msys.hal.manager.v1"
HAL_INVENTORY_TIMEOUT_SECONDS = 35
HAL_STATE_TIMEOUT_SECONDS = 4


def _payload(response: object) -> dict[str, Any]:
    if not isinstance(response, dict) or response.get("type") != "return":
        return {}
    payload = response.get("payload")
    return payload if isinstance(payload, dict) else {}


def battery(client: MsysClient) -> dict[str, Any]:
    """Read power exclusively through the replaceable HAL contract."""

    unavailable: dict[str, Any] = {
        "available": False,
        "capacity": None,
        "status": None,
        "source": HAL_MANAGER_INTERFACE,
    }
    try:
        inventory = _payload(client.call_interface(
            HAL_MANAGER_INTERFACE,
            "inventory",
            {"domains": ["power"]},
            # A cold manager may have to start and describe every catalog HAL
            # provider before it can filter the power domain.  This happens
            # after status-agent readiness and must not be mistaken for a
            # provider failure merely because it exceeds a short state read.
            timeout=HAL_INVENTORY_TIMEOUT_SECONDS,
            idempotent=True,
        ))
        devices = inventory.get("devices", [])
        if not isinstance(devices, list):
            return unavailable
        candidates = [
            item for item in devices
            if isinstance(item, dict)
            and item.get("domain") == "power"
            and item.get("available") is True
            and isinstance(item.get("id"), str)
        ]
        if not candidates:
            return unavailable
        # Prefer a battery when AC/USB supplies share the same power domain.
        selected = next((
            item for item in candidates
            if str(item.get("metadata", {}).get("type", "")).lower() == "battery"
        ), candidates[0])
        state = _payload(client.call_interface(
            HAL_MANAGER_INTERFACE,
            "get_state",
            {"id": selected["id"]},
            timeout=HAL_STATE_TIMEOUT_SECONDS,
            idempotent=True,
        )).get("state", {})
        values = state.get("values", {}) if isinstance(state, dict) else {}
        if not isinstance(values, dict):
            return unavailable
        capacity = values.get("capacity_percent")
        if isinstance(capacity, bool) or not isinstance(capacity, int) or not 0 <= capacity <= 100:
            capacity = None
        status = values.get("status")
        if not isinstance(status, str):
            status = None
        return {
            "available": bool(state.get("available", True)) if isinstance(state, dict) else True,
            "capacity": capacity,
            "status": status,
            "device": selected["id"],
            "provider": state.get("provider") or selected.get("provider"),
            "source": HAL_MANAGER_INTERFACE,
        }
    except (EOFError, OSError, RuntimeError, TimeoutError, ValueError):
        return unavailable


def snapshot(client: MsysClient) -> dict[str, Any]:
    return {
        "time": _dt.datetime.now().strftime("%H:%M"),
        "display": os.environ.get("DISPLAY", ""),
        "battery": battery(client),
    }


def main() -> int:
    client = MsysClient.from_env()
    print("status-agent: hello", flush=True)
    client.hello()
    print("status-agent: ready", flush=True)
    client.ready()
    client.event("msys.status.ready", {"component": client.component_id})
    while True:
        client.event("msys.status.tick", snapshot(client))
        time.sleep(10)


if __name__ == "__main__":
    raise SystemExit(main())
