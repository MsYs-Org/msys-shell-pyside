from __future__ import annotations

import os
import queue
import threading
from dataclasses import dataclass
from typing import Any, Mapping

from msys_sdk import MsysClient

from .localization import shell_text
from msys_sdk.ui_fonts import configure_tk_fonts, font_spec


SCREEN_SHIELD_TOPIC = "msys.role.screen-shield"
STATUS_SCHEMA = "msys.screen-shield.status.v1"
TOUCH_DISMISS_ENV = "MSYS_SCREEN_SHIELD_TOUCH_DISMISS"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "enabled"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", "disabled"})


def boolean_setting(value: object, *, default: bool) -> bool:
    """Parse a bounded manifest/environment boolean without truthy surprises."""

    if value is None or value == "":
        return bool(default)
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return bool(default)


def touch_dismiss_from_env(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return boolean_setting(values.get(TOUCH_DISMISS_ENV), default=True)


@dataclass(frozen=True, slots=True)
class ShieldVisibilityCommand:
    revision: int
    visible: bool
    reason: str


class ScreenShieldService:
    """Thread-safe typed role behavior, independent of the Tk presentation."""

    def __init__(
        self,
        actions: queue.Queue[tuple[str, Any]],
        *,
        touch_dismiss_enabled: bool = True,
    ) -> None:
        self.actions = actions
        self.touch_dismiss_enabled = bool(touch_dismiss_enabled)
        self._lock = threading.RLock()
        self._visible = False
        self._revision = 0
        self._last_reason = "startup"

    @property
    def visible(self) -> bool:
        with self._lock:
            return self._visible

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def _status_locked(self, *, changed: bool | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema": STATUS_SCHEMA,
            "visible": self._visible,
            "revision": self._revision,
            "touch_dismiss_enabled": self.touch_dismiss_enabled,
            "last_reason": self._last_reason,
        }
        if changed is not None:
            result["changed"] = bool(changed)
        return result

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._status_locked()

    def set_visible(self, visible: bool, *, reason: str) -> dict[str, Any]:
        desired = bool(visible)
        with self._lock:
            if desired == self._visible:
                return self._status_locked(changed=False)
            self._revision += 1
            self._visible = desired
            self._last_reason = str(reason)
            command = ShieldVisibilityCommand(
                revision=self._revision,
                visible=desired,
                reason=self._last_reason,
            )
            # Queue while holding the state lock so concurrent callers cannot
            # reorder their window commands relative to their revisions.
            self.actions.put(("visibility", command))
            return self._status_locked(changed=True)

    def show(self, *, reason: str = "rpc-show") -> dict[str, Any]:
        return self.set_visible(True, reason=reason)

    def hide(self, *, reason: str = "rpc-hide") -> dict[str, Any]:
        return self.set_visible(False, reason=reason)

    def toggle(self, *, reason: str = "rpc-toggle") -> dict[str, Any]:
        with self._lock:
            desired = not self._visible
            self._revision += 1
            self._visible = desired
            self._last_reason = str(reason)
            command = ShieldVisibilityCommand(
                revision=self._revision,
                visible=desired,
                reason=self._last_reason,
            )
            self.actions.put(("visibility", command))
            return self._status_locked(changed=True)

    def surface_lost(self, *, reason: str) -> dict[str, Any]:
        """Reconcile logical state after an external unmap/destroy.

        No hide command is queued: the presentation has already disappeared.
        Incrementing the revision also invalidates any older command that may
        still be waiting in the Tk queue.
        """

        with self._lock:
            if not self._visible:
                return self._status_locked(changed=False)
            self._revision += 1
            self._visible = False
            self._last_reason = str(reason)
            return self._status_locked(changed=True)

    def dismiss_from_touch(self) -> dict[str, Any]:
        if not self.touch_dismiss_enabled:
            with self._lock:
                return self._status_locked(changed=False)
        return self.hide(reason="touch-dismiss")

    def handle_event(self, message: Mapping[str, Any]) -> bool:
        """Compatibility path for the original action broadcast topic."""

        if message.get("topic") != SCREEN_SHIELD_TOPIC:
            return False
        payload = message.get("payload", {})
        if not isinstance(payload, Mapping):
            return False
        action = str(payload.get("action", "")).strip().lower()
        if action == "show":
            self.show(reason="event-show")
        elif action == "hide":
            self.hide(reason="event-hide")
        elif action == "toggle":
            self.toggle(reason="event-toggle")
        else:
            return False
        return True

    def handle_call(self, message: Mapping[str, Any]) -> dict[str, Any]:
        try:
            request_id = int(message.get("id", 0))
        except (TypeError, ValueError, OverflowError):
            request_id = 0
        method = str(message.get("method", ""))
        payload = message.get("payload", {})
        if not isinstance(payload, Mapping):
            return self._error(request_id, "BAD_REQUEST", "payload must be an object")

        if method == "show":
            result = self.show()
        elif method == "hide":
            result = self.hide()
        elif method == "toggle":
            result = self.toggle()
        elif method == "status":
            result = self.status()
        else:
            return self._error(request_id, "NO_METHOD", method)
        return {"type": "return", "id": request_id, "payload": result}

    @staticmethod
    def _error(request_id: int, code: str, message: str) -> dict[str, Any]:
        return {"type": "error", "id": request_id, "code": code, "message": message}


class ScreenShieldTkUi:
    """A recreatable fullscreen surface driven only by service commands."""

    def __init__(self, root: Any, service: ScreenShieldService) -> None:
        self.root = root
        self.service = service
        self.panel: Any | None = None
        self.label: Any | None = None
        self._render_revision = 0
        self._expected_destroy = False

    def _screen_geometry(self) -> str:
        width = max(1, int(self.root.winfo_screenwidth()))
        height = max(1, int(self.root.winfo_screenheight()))
        return f"{width}x{height}+0+0"

    def _panel_alive(self) -> bool:
        if self.panel is None:
            return False
        try:
            return bool(self.panel.winfo_exists())
        except Exception:
            return False

    def _touch(self, _event: Any) -> str:
        self.service.dismiss_from_touch()
        # Even when dismissal is disabled, consume the pointer so it cannot
        # leak through the full-screen ownership surface.
        return "break"

    def _on_destroy(self, event: Any) -> None:
        panel = self.panel
        if panel is None or getattr(event, "widget", None) is not panel:
            return
        expected = self._expected_destroy
        self.panel = None
        self.label = None
        self._expected_destroy = False
        if not expected:
            self.service.surface_lost(reason="window-destroyed")

    def _reconcile_unmap(self, panel: Any) -> None:
        if panel is not self.panel or not self.service.visible:
            return
        try:
            state = str(panel.state())
        except Exception:
            state = "destroyed"
        if state not in {"normal", "zoomed"}:
            self.service.surface_lost(reason="window-unmapped")

    def _on_unmap(self, event: Any) -> None:
        panel = self.panel
        if panel is None or getattr(event, "widget", None) is not panel:
            return
        try:
            self.root.after_idle(lambda: self._reconcile_unmap(panel))
        except Exception:
            self._reconcile_unmap(panel)

    def _ensure_panel(self) -> None:
        if self._panel_alive():
            return
        import tkinter as tk

        panel = tk.Toplevel(
            self.root,
            class_=os.environ.get("MSYS_WINDOW_IDENTITY", "MsysScreenShield"),
        )
        self.panel = panel
        panel.title(shell_text("shield.window_title"))
        panel.configure(bg="#101010")
        panel.attributes("-topmost", True)
        panel.resizable(True, True)
        panel.protocol("WM_DELETE_WINDOW", lambda: self.service.hide(reason="window-close"))
        panel.bind("<ButtonRelease-1>", self._touch, add="+")
        panel.bind("<Destroy>", self._on_destroy, add="+")
        panel.bind("<Unmap>", self._on_unmap, add="+")
        label = tk.Label(
            panel,
            text=shell_text("shield.message"),
            bg="#101010",
            fg="white",
            font=font_spec(panel, 16, "bold"),
            justify="center",
        )
        self.label = label
        label.pack(expand=True, fill="both")
        label.bind("<ButtonRelease-1>", self._touch, add="+")
        panel.withdraw()

    def apply_visibility(self, command: ShieldVisibilityCommand) -> bool:
        # A destroyed/unmapped surface increments the service revision without
        # placing another UI command. Never replay a now-stale show afterwards.
        if command.revision != self.service.revision:
            return False
        self._render_revision = command.revision
        if not command.visible:
            if self._panel_alive():
                assert self.panel is not None
                self.panel.withdraw()
                self.panel.attributes("-topmost", False)
            return True

        try:
            self._ensure_panel()
            assert self.panel is not None
            geometry = self._screen_geometry()
            self.panel.geometry(geometry)
            self.panel.update_idletasks()
            self.panel.deiconify()
            self.panel.lift()
            self.panel.attributes("-topmost", True)
            self.panel.geometry(geometry)
            self.panel.update()
            return True
        except Exception as exc:
            print(f"screen-shield: cannot map surface: {exc}", flush=True)
            self.service.surface_lost(reason="window-map-failed")
            return False

    def shutdown(self) -> None:
        self.service.surface_lost(reason="shutdown")
        if self._panel_alive():
            assert self.panel is not None
            self._expected_destroy = True
            try:
                self.panel.withdraw()
                self.panel.destroy()
            except Exception:
                pass
        self.panel = None
        self.label = None
        try:
            self.root.destroy()
        except Exception:
            pass


def run_tk() -> int:
    import tkinter as tk

    # The long-running role host never owns screen space. The fullscreen
    # Toplevel exists only while the typed state says the shield is visible.
    root = tk.Tk(className=os.environ.get("MSYS_WINDOW_IDENTITY", "MsysScreenShield"))
    configure_tk_fonts(root, default_size=10)
    root.title("msys-screen-shield-host")
    root.geometry("1x1+0+0")
    root.withdraw()
    root.update_idletasks()

    actions: queue.Queue[tuple[str, Any]] = queue.Queue()
    service = ScreenShieldService(
        actions,
        touch_dismiss_enabled=touch_dismiss_from_env(),
    )
    ui = ScreenShieldTkUi(root, service)
    client = MsysClient.from_env()
    client.hello()
    client.subscribe(SCREEN_SHIELD_TOPIC)
    client.ready()
    client.event(
        "msys.role.ready",
        {
            "role": "screen-shield",
            "component": client.component_id,
            "visible": False,
            "touch_dismiss_enabled": service.touch_dismiss_enabled,
        },
    )

    def ipc_loop() -> None:
        try:
            while True:
                message = client.recv(timeout=None)
                if not message or message.get("type") in {"eof", "shutdown"}:
                    actions.put(("shutdown", None))
                    return
                if message.get("type") == "event":
                    service.handle_event(message)
                elif message.get("type") == "call":
                    client.send(service.handle_call(message))
        except Exception as exc:
            print(f"screen-shield: IPC failed: {exc}", flush=True)
            actions.put(("shutdown", None))

    def pump() -> None:
        while True:
            try:
                action, value = actions.get_nowait()
            except queue.Empty:
                break
            if action == "visibility":
                ui.apply_visibility(value)
            elif action == "shutdown":
                ui.shutdown()
                return
        root.after(30, pump)

    threading.Thread(target=ipc_loop, name="msys-screen-shield-ipc", daemon=True).start()
    root.after(30, pump)
    root.mainloop()
    return 0


def main() -> int:
    try:
        return run_tk()
    except Exception as exc:
        print(f"screen-shield: Tk failed: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
