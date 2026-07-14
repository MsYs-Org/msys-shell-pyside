from __future__ import annotations

import os

from msys_sdk import MsysClient


def main() -> int:
    client = MsysClient.from_env()
    client.hello()
    client.subscribe("msys.role.window-policy")
    client.ready()
    client.event("msys.role.ready", {
        "role": "window-policy",
        "component": client.component_id,
        "display": os.environ.get("DISPLAY", ""),
        "mode": os.environ.get("MSYS_WINDOW_POLICY", "unmanaged"),
    })

    def on_event(message: dict) -> None:
        print(f"window-policy event: {message}", flush=True)

    print(
        "window-policy ready "
        f"display={os.environ.get('DISPLAY', '')} "
        f"mode={os.environ.get('MSYS_WINDOW_POLICY', 'unmanaged')}",
        flush=True,
    )
    client.run(on_event=on_event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
