#!/usr/bin/env python3
"""Measure notification-center cold UI phases without starting mIPC."""

from __future__ import annotations

import json
import queue
import tempfile
import time


def main() -> int:
    started = time.perf_counter()
    previous = started
    phases: list[dict[str, float | str]] = []

    def mark(name: str) -> None:
        nonlocal previous
        current = time.perf_counter()
        phases.append({"name": name, "milliseconds": round((current - previous) * 1000, 2)})
        previous = current

    import msys_shell_pyside.notification_center as notification_center

    mark("module-import")
    import tkinter as tk

    mark("tk-import")
    root = tk.Tk(className="MsysNotificationCenter")
    root.withdraw()
    mark("tk-root")
    notification_center.configure_notification_fonts(root, default_size=10)
    mark("font-configuration")
    root.title("msys-notification-benchmark")
    root.update_idletasks()
    mark("host-idle")

    with tempfile.TemporaryDirectory() as temporary:
        store = notification_center.NotificationHistoryStore(
            notification_center.Path(temporary) / "history.json",
            100,
        )
        service = notification_center.NotificationCenterService(store, queue.Queue())
        ui = notification_center.NotificationCenterUi(root, service, lambda: None)
        mark("model-and-store")
        ui._create_panel()
        mark("panel-create")
        ui.show()
        mark("show-return")
        root.update()
        mark("first-update")
        ui.hide()
    root.destroy()
    print(
        json.dumps(
            {
                "phases": phases,
                "total_milliseconds": round((time.perf_counter() - started) * 1000, 2),
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
