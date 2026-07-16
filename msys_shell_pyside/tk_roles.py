from __future__ import annotations

import argparse
import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from msys_sdk import MsysClient

from .adaptive import adaptive_panel_geometry, edge_bar_rect, full_screen_rect
from .navigation_gesture import (
    PILL_RECENTS_HOLD_MS,
    PillGestureStateMachine,
    PillGestureUpdate,
    infer_navigation_edge,
    inward_distance,
)
from .localization import shell_text
from .task_switcher import task_text
from msys_sdk.ui_fonts import configure_tk_fonts, font_spec


@dataclass(frozen=True)
class RoleSpec:
    name: str
    title: str
    geometry: str
    bg: str
    fg: str = "white"
    text: str = ""


ROLES = {
    "system-chrome": RoleSpec(
        name="system-chrome",
        title=shell_text("chrome.window_title"),
        geometry="320x42+0+0",
        bg="#20252b",
        text=shell_text("chrome.initial"),
    ),
    "notification-presenter": RoleSpec(
        name="notification-presenter",
        title=shell_text("toast.window_title"),
        geometry="300x92+10+54",
        bg="#263238",
        text=shell_text("notification.initial"),
    ),
    "screen-shield": RoleSpec(
        name="screen-shield",
        title=shell_text("shield.window_title"),
        geometry="320x480+0+0",
        bg="#101010",
        text=shell_text("shield.message"),
    ),
    "navigation-bar": RoleSpec(
        name="navigation-bar",
        title=shell_text("navigation.window_title"),
        geometry="320x42+0+438",
        bg="#11151a",
        text="",
    ),
}


NAVIGATION_ACTIONS = ("back", "home", "apps")
NAVIGATION_TYPED_METHODS = ("navigation_action", "navigate")
NAVIGATION_INPUTS = frozenset({"button", "swipe"})
MIN_TOAST_TIMEOUT_MS = 500
DEFAULT_TOAST_TIMEOUT_MS = 2500
MAX_TOAST_TIMEOUT_MS = 6000
MAX_TOAST_MESSAGE_CHARS = 512


def notification_timeout_ms(
    value: object,
    fallback: object = DEFAULT_TOAST_TIMEOUT_MS,
) -> int:
    """Return a safe toast lifetime without trusting event or env input."""

    try:
        default = (
            int(fallback)
            if not isinstance(fallback, bool)
            else DEFAULT_TOAST_TIMEOUT_MS
        )
    except (TypeError, ValueError, OverflowError):
        default = DEFAULT_TOAST_TIMEOUT_MS
    try:
        timeout = default if value is None else int(value)
        if isinstance(value, bool):
            timeout = default
    except (TypeError, ValueError, OverflowError):
        timeout = default
    return min(MAX_TOAST_TIMEOUT_MS, max(MIN_TOAST_TIMEOUT_MS, timeout))


def notification_message(value: object) -> str:
    text = str(value or "").strip()
    if len(text) <= MAX_TOAST_MESSAGE_CHARS:
        return text
    return text[: MAX_TOAST_MESSAGE_CHARS - 1] + "\N{HORIZONTAL ELLIPSIS}"


def role_geometry(root, role: str, fallback: str) -> str:
    """Choose useful first-map geometry before X11 policy reflows a role."""

    width = max(1, int(root.winfo_screenwidth()))
    height = max(1, int(root.winfo_screenheight()))
    if role == "screen-shield":
        return full_screen_rect(width, height).geometry()
    if role == "system-chrome":
        return edge_bar_rect(width, height, "top").geometry()
    if role == "navigation-bar":
        edge = "right" if width > height else "bottom"
        return edge_bar_rect(width, height, edge).geometry()
    return fallback


def navigation_is_vertical(width: int, height: int) -> bool:
    """Return whether policy placed navigation on a side edge."""

    return max(int(height), 1) > max(int(width), 1)


def navigation_pill_visual(
    width: int,
    height: int,
) -> tuple[int, int, int, int, int]:
    """Return a small, bounded pill line for either navigation edge."""

    safe_width = max(1, int(width))
    safe_height = max(1, int(height))
    thickness = max(1, min(4, safe_width, safe_height))
    if navigation_is_vertical(safe_width, safe_height):
        length = max(1, min(48, safe_height - 8))
        center_x = safe_width // 2
        center_y = safe_height // 2
        start_y = max(0, center_y - length // 2)
        end_y = min(safe_height - 1, center_y + (length + 1) // 2)
        return center_x, start_y, center_x, end_y, thickness
    length = max(1, min(48, safe_width - 8))
    center_x = safe_width // 2
    center_y = safe_height // 2
    start_x = max(0, center_x - length // 2)
    end_x = min(safe_width - 1, center_x + (length + 1) // 2)
    return start_x, center_y, end_x, center_y, thickness


def _blend_color(start: str, end: str, progress: float) -> str:
    amount = min(1.0, max(0.0, float(progress)))
    start_rgb = tuple(int(start[index:index + 2], 16) for index in (1, 3, 5))
    end_rgb = tuple(int(end[index:index + 2], 16) for index in (1, 3, 5))
    channels = tuple(
        round(source + (target - source) * amount)
        for source, target in zip(start_rgb, end_rgb)
    )
    return "#" + "".join(f"{channel:02x}" for channel in channels)


def navigation_pill_motion_visual(
    width: int,
    height: int,
    edge: str,
    inward: float,
    progress: float,
) -> tuple[int, int, int, int, int, str]:
    """Return the bounded follow/stretch/accent visual for a pill drag."""

    safe_width = max(1, int(width))
    safe_height = max(1, int(height))
    x1, y1, x2, y2, thickness = navigation_pill_visual(safe_width, safe_height)
    amount = min(1.0, max(0.0, float(progress)))
    offset = min(8, max(0, round(float(inward) * 0.30)))
    stretch = round(16 * amount)
    normalized_edge = infer_navigation_edge(
        safe_width,
        safe_height,
        preferred=edge,
    )
    if normalized_edge in {"left", "right"}:
        y1 = max(0, y1 - stretch // 2)
        y2 = min(safe_height - 1, y2 + (stretch + 1) // 2)
        direction = 1 if normalized_edge == "left" else -1
        x1 = x2 = max(0, min(safe_width - 1, x1 + direction * offset))
    else:
        x1 = max(0, x1 - stretch // 2)
        x2 = min(safe_width - 1, x2 + (stretch + 1) // 2)
        direction = 1 if normalized_edge == "top" else -1
        y1 = y2 = max(0, min(safe_height - 1, y1 + direction * offset))
    thickness = min(
        safe_width,
        safe_height,
        max(1, thickness + round(amount * 2)),
    )
    color = _blend_color("#e7ebf0", "#82b1ff", amount)
    return x1, y1, x2, y2, thickness, color


def navigation_action_at(
    x: int,
    y: int,
    width: int,
    height: int,
) -> str:
    """Map one release to Back/Home/Apps along either navigation axis."""

    vertical = navigation_is_vertical(width, height)
    extent = max(int(height if vertical else width), 1)
    position = int(y if vertical else x)
    index = max(0, min(2, position * 3 // extent))
    return NAVIGATION_ACTIONS[index]


def navigation_gesture_action(
    start_x: int | None,
    start_y: int | None,
    end_x: int,
    end_y: int,
    width: int,
    height: int,
    *,
    threshold: int = 18,
    edge: str | None = None,
) -> str:
    """Map an inward pill swipe to close, with release-only fallback.

    Mobile policy normally uses the bottom while landscape uses a side edge;
    explicit policy may also select top or left. CH347 can occasionally
    deliver only a usable release, in which case the normal three-zone action
    remains available.
    """

    if start_x is not None and start_y is not None:
        distance = max(8, int(threshold))
        live_edge = edge or infer_navigation_edge(width, height)
        if inward_distance(
            live_edge,
            int(start_x),
            int(start_y),
            int(end_x),
            int(end_y),
        ) >= distance:
            return "close"
    return navigation_action_at(end_x, end_y, width, height)


def navigation_window_method(action: str) -> str:
    """Keep three-button Back distinct from the pill's explicit close swipe."""

    methods = {"back": "back", "close": "close_active", "home": "home"}
    try:
        return methods[str(action)]
    except KeyError as exc:
        raise ValueError(f"unsupported navigation action: {action}") from exc


@dataclass(frozen=True, slots=True)
class NavigationDispatchResult:
    """One completed typed or compatibility navigation transaction."""

    payload: dict[str, Any]
    method: str
    legacy: bool = False
    warning: str = ""


@dataclass(frozen=True, slots=True)
class NavigationFeedback:
    """Small status update rendered inside the navigation surface itself."""

    action: str
    ok: bool
    detail: str = ""


class NavigationDispatchError(RuntimeError):
    pass


PublicCaller = Callable[..., dict[str, Any]]


def navigation_method_unavailable(response: object, method: str) -> bool:
    """Recognise an older provider without mistaking action failure for it."""

    if not isinstance(response, dict):
        return False
    packet_type = str(response.get("type") or "")
    code = str(response.get("code") or "").strip().upper()
    message = str(response.get("message") or "").strip().lower()
    if packet_type == "error":
        if code in {"NO_METHOD", "METHOD_NOT_FOUND", "NOT_IMPLEMENTED"}:
            return True
        return str(method).lower() in message and any(
            marker in message
            for marker in ("unknown method", "no method", "not implemented", "unsupported method")
        )
    if packet_type != "return":
        return False
    payload = response.get("payload", {})
    if not isinstance(payload, dict) or payload.get("schema") == "msys.navigation-action.v1":
        return False
    detail = " ".join(
        str(payload.get(key) or "").strip().lower()
        for key in ("reason", "error", "message")
    )
    return payload.get("ok") is False and str(method).lower() in detail and any(
        marker in detail
        for marker in ("unknown method", "no method", "not implemented", "unsupported method")
    )


def _require_navigation_success(payload: dict[str, Any], operation: str) -> None:
    if payload.get("ok") is not False:
        return
    detail = (
        payload.get("reason")
        or payload.get("error")
        or payload.get("message")
        or "action rejected"
    )
    raise NavigationDispatchError(f"{operation} failed: {detail}")


def _legacy_navigation_action(
    action: str,
    input_kind: str,
    *,
    public_call: PublicCaller,
) -> NavigationDispatchResult:
    """Compatibility path for pre-navigation-action window managers."""

    if action == "apps":
        try:
            response = public_call("role:task-switcher", "show", {}, timeout=7)
            payload = public_return_payload(response, "task-switcher.show")
            _require_navigation_success(payload, "task-switcher.show")
            return NavigationDispatchResult(payload, "task-switcher.show", legacy=True)
        except Exception as show_error:
            try:
                response = public_call("role:window-manager", "recents", {}, timeout=7)
                payload = public_return_payload(response, "window-manager.recents")
                _require_navigation_success(payload, "window-manager.recents")
            except Exception as recents_error:
                raise NavigationDispatchError(
                    f"legacy Apps failed: {show_error}; recents fallback failed: {recents_error}"
                ) from recents_error
            windows = payload.get("windows", [])
            count = len(windows) if isinstance(windows, list) else 0
            return NavigationDispatchResult(
                payload,
                "window-manager.recents",
                legacy=True,
                warning=f"task switcher unavailable; {count} recent task(s)",
            )

    method = navigation_window_method("close" if action == "close" else action)
    response = public_call("role:window-manager", method, {}, timeout=7)
    payload = public_return_payload(response, f"window-manager.{method}")
    _require_navigation_success(payload, f"window-manager.{method}")
    return NavigationDispatchResult(payload, f"window-manager.{method}", legacy=True)


def dispatch_navigation_action(
    action: str,
    input_kind: str = "button",
    *,
    public_call: PublicCaller = MsysClient.public_call,
) -> NavigationDispatchResult:
    """Use window-manager v1 navigation, then explicit compatibility paths.

    A pill's inward ``close`` gesture is expressed as typed Back with
    ``input=swipe``. On an older provider it retains the historical
    ``close_active`` call. Only a proven missing method falls back, preventing
    a timeout or semantic rejection from executing the same action twice.
    """

    logical_action = str(action or "").strip().lower()
    if logical_action not in {*NAVIGATION_ACTIONS, "close"}:
        raise ValueError(f"unsupported navigation action: {action}")
    normalized_input = str(input_kind or "").strip().lower()
    if normalized_input not in NAVIGATION_INPUTS:
        raise ValueError(f"unsupported navigation input: {input_kind}")
    if logical_action == "close":
        normalized_input = "swipe"
    typed_action = "back" if logical_action == "close" else logical_action
    request = {"action": typed_action, "input": normalized_input}

    for method in NAVIGATION_TYPED_METHODS:
        response = public_call("role:window-manager", method, request, timeout=7)
        if navigation_method_unavailable(response, method):
            continue
        payload = public_return_payload(response, f"window-manager.{method}")
        _require_navigation_success(payload, f"window-manager.{method}")
        return NavigationDispatchResult(payload, f"window-manager.{method}")

    return _legacy_navigation_action(
        logical_action,
        normalized_input,
        public_call=public_call,
    )


def perform_navigation_action(
    action: str,
    input_kind: str = "button",
    *,
    public_call: PublicCaller = MsysClient.public_call,
    feedback: Callable[[NavigationFeedback], None] | None = None,
) -> NavigationDispatchResult | None:
    """Execute one navigation transaction and report without an overlay."""

    try:
        result = dispatch_navigation_action(
            action,
            input_kind,
            public_call=public_call,
        )
    except Exception as exc:
        print(f"navigation {action} failed: {exc}", flush=True)
        if feedback is not None:
            feedback(NavigationFeedback(action=action, ok=False, detail=str(exc)))
        return None
    print(
        f"navigation {action} response via {result.method}: {result.payload}",
        flush=True,
    )
    if feedback is not None:
        feedback(NavigationFeedback(
            action=action,
            ok=not bool(result.warning),
            detail=result.warning,
        ))
    return result


def bind_navigation_button(widget, action) -> None:
    """Bind one concrete surface and stop duplicate Toplevel dispatch."""

    normal = "#202833"
    pressed = "#34445a"

    def restore() -> None:
        widget.configure(bg=normal, relief="raised")

    def on_press(_event) -> str:
        widget.configure(bg=pressed, relief="sunken")
        return "break"

    def on_release(_event) -> str:
        restore()
        action()
        return "break"

    widget.bind("<ButtonPress-1>", on_press, add="+")
    widget.bind("<ButtonRelease-1>", on_release, add="+")
    widget.bind("<Leave>", lambda _event: restore(), add="+")


def start_background_action(name: str, action):
    """Start one broker action without ever running it on Tk's event thread."""

    worker = threading.Thread(
        target=action,
        name=f"msys-{name}",
        daemon=True,
    )
    worker.start()
    return worker


def public_return_payload(response: object, operation: str) -> dict:
    """Require one successful public mIPC return instead of hiding errors."""

    if not isinstance(response, dict):
        raise RuntimeError(f"{operation} returned a non-object response")
    if response.get("type") != "return":
        code = str(response.get("code") or "REMOTE_ERROR")
        message = str(response.get("message") or "remote call failed")
        detail = f"{code}: {message}"
        raise RuntimeError(f"{operation} failed: {detail}")
    payload = response.get("payload", {})
    if not isinstance(payload, dict):
        raise RuntimeError(f"{operation} returned a non-object payload")
    return payload


def system_status_text(payload: dict) -> str:
    clock = payload.get("time", "--:--")
    if not isinstance(clock, str) or not clock:
        clock = "--:--"
    battery = payload.get("battery", {})
    if isinstance(battery, dict) and battery.get("available"):
        capacity = battery.get("capacity")
        if isinstance(capacity, int) and not isinstance(capacity, bool) and 0 <= capacity <= 100:
            return shell_text(
                "chrome.status.battery",
                {"time": clock, "capacity": capacity},
            )
    return shell_text("chrome.status", {"time": clock})


def local_system_status_text(battery: dict | None = None) -> str:
    """Keep the resident chrome clock useful without a status-agent process."""

    return system_status_text({
        "time": time.strftime("%H:%M"),
        "battery": battery if isinstance(battery, dict) else {},
    })


def ipc_connect(role: str) -> MsysClient:
    client = MsysClient.from_env()
    print(f"{role}: hello", flush=True)
    client.hello()
    return client


def mark_ipc_ready(client: MsysClient, role: str) -> None:
    print(f"{role}: ready", flush=True)
    client.ready()
    client.subscribe(f"msys.role.{role}")
    if role == "system-chrome":
        client.subscribe("msys.status.tick")
    client.event("msys.role.ready", {"role": role, "component": client.component_id})


def run_headless(role: str) -> int:
    client = ipc_connect(role)
    mark_ipc_ready(client, role)
    print(f"{role} headless DISPLAY={os.environ.get('DISPLAY', '')}", flush=True)
    client.run(on_event=lambda msg: print(f"{role} event: {msg}", flush=True))
    return 0


def run_tk(role: str, visible: bool = True) -> int:
    import tkinter as tk

    spec = ROLES[role]
    hide_after_id: str | None = None

    root = tk.Tk(className=os.environ.get("MSYS_WINDOW_IDENTITY", "MsysRole"))
    configure_tk_fonts(root, default_size=10)
    client = ipc_connect(role)
    if role == "notification-presenter":
        return run_notification_tk(client, spec, root)
    root.title(spec.title)
    root.geometry(role_geometry(root, role, spec.geometry))
    root.configure(bg=spec.bg)
    root.attributes("-topmost", True)
    adaptive_role = role in {"system-chrome", "navigation-bar", "screen-shield"}
    root.resizable(adaptive_role, adaptive_role)

    label: tk.Label | None = None
    keyboard_zone: tk.Label | None = None
    chrome_battery: dict = {}

    if role == "navigation-bar":
        build_navigation_bar(root, spec, client)
    else:
        label = tk.Label(
            root,
            text=spec.text,
            bg=spec.bg,
            fg=spec.fg,
            font=font_spec(root, 11, "bold"),
            justify="center",
        )
        label.pack(expand=True, fill="both")
        if role == "system-chrome":
            keyboard_zone = tk.Label(
                root,
                text="⌨",
                bg="#2b3642",
                fg="white",
                font=font_spec(root, 13, "bold"),
                width=3,
                cursor="hand2",
            )
            keyboard_zone.place(relx=1.0, rely=0.5, x=-4, anchor="e")
            # Reuse the component's already-authenticated channel.  Opening a
            # cold on-demand panel no longer pays for an extra public-socket
            # handshake before Core can start the selected role.
            bind_system_chrome_notification_toggle(root, client.call)

    if role == "screen-shield" and not visible:
        root.withdraw()
    if role == "screen-shield" and visible:
        root.geometry(role_geometry(root, role, spec.geometry))
        root.update_idletasks()
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)
    if role == "notification-presenter":
        root.withdraw()

    # Tk() proves that DISPLAY is reachable; update() proves that a visible
    # role has actually been created and mapped.  Only then advertise ready.
    root.update_idletasks()
    if role != "screen-shield" or visible:
        root.deiconify()
        root.update()
    mark_ipc_ready(client, role)

    def show_shield() -> None:
        geometry = role_geometry(root, role, spec.geometry)
        root.geometry(geometry)
        root.update_idletasks()
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)
        root.geometry(geometry)

    def hide_notification() -> None:
        nonlocal hide_after_id
        hide_after_id = None
        root.withdraw()

    def show_notification(message: str, timeout_ms: int = 3500) -> None:
        nonlocal hide_after_id
        if label is not None:
            label.configure(text=f"{shell_text('notification.title')}\n{message}")
        if hide_after_id is not None:
            try:
                root.after_cancel(hide_after_id)
            except tk.TclError:
                pass
        root.geometry(spec.geometry)
        root.update_idletasks()
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)
        root.geometry(spec.geometry)
        root.update_idletasks()
        root.update()
        hide_after_id = root.after(notification_timeout_ms(timeout_ms), hide_notification)

    def refresh_chrome_clock() -> None:
        if role != "system-chrome" or label is None:
            return
        label.configure(text=local_system_status_text(chrome_battery))
        root.after(1000, refresh_chrome_clock)

    if role == "notification-presenter":
        root.bind("<ButtonPress-1>", lambda _event: hide_notification())
        if label is not None:
            label.bind("<ButtonPress-1>", lambda _event: hide_notification())

    def apply_event(msg: dict) -> None:
        nonlocal chrome_battery
        payload = msg.get("payload", {})
        action = payload.get("action")
        if role == "screen-shield":
            if action == "show":
                show_shield()
            if action == "hide":
                root.withdraw()
        if role == "notification-presenter" and label is not None and "message" in payload:
            timeout_ms = notification_timeout_ms(
                payload.get("timeout_ms"),
                os.environ.get("MSYS_NOTIFY_TIMEOUT_MS", str(DEFAULT_TOAST_TIMEOUT_MS)),
            )
            print(f"notification-presenter: show {payload['message']}", flush=True)
            show_notification(str(payload["message"]), timeout_ms)
        if role == "system-chrome" and label is not None and msg.get("topic") == "msys.status.tick":
            battery = payload.get("battery")
            if isinstance(battery, dict):
                chrome_battery = battery
            text = system_status_text(payload)
            label.configure(text=text)

    incoming: queue.SimpleQueue[dict] = queue.SimpleQueue()

    def pump_events() -> None:
        while True:
            try:
                message = incoming.get_nowait()
            except queue.Empty:
                break
            apply_event(message)
        root.after(30, pump_events)

    threading.Thread(target=lambda: client.run(on_event=incoming.put), daemon=True).start()
    if role == "system-chrome":
        root.after(0, refresh_chrome_clock)
    root.after(30, pump_events)
    root.mainloop()
    return 0


def run_notification_tk(client: MsysClient, spec: RoleSpec, root) -> int:
    import tkinter as tk

    root.withdraw()
    root.title("msys-notification-host")
    root.geometry("1x1+0+0")
    root.update_idletasks()
    mark_ipc_ready(client, "notification-presenter")
    active_toasts: list[tk.Toplevel] = []
    hide_after_id: str | None = None
    events: queue.Queue[tuple[str, int]] = queue.Queue()

    def close_toast(toast: tk.Toplevel, *, cancel_timer: bool = True) -> None:
        nonlocal hide_after_id
        if cancel_timer and hide_after_id is not None:
            try:
                root.after_cancel(hide_after_id)
            except tk.TclError:
                pass
            hide_after_id = None
        elif not cancel_timer:
            hide_after_id = None
        try:
            toast.destroy()
        except tk.TclError:
            pass
        try:
            active_toasts.remove(toast)
        except ValueError:
            pass

    def show(message: str, timeout_ms: int) -> None:
        nonlocal hide_after_id
        for toast in list(active_toasts):
            close_toast(toast)
        active_toasts.clear()

        toast = tk.Toplevel(
            root,
            class_=os.environ.get("MSYS_WINDOW_IDENTITY", "MsysNotificationPresenter"),
        )
        toast.title(spec.title)
        toast.geometry(adaptive_panel_geometry(
            root,
            width_ratio=0.92,
            height_ratio=0.20,
            minimum_width=250,
            minimum_height=70,
            maximum_width=420,
            maximum_height=150,
            anchor="top-right",
        ))
        toast.configure(bg=spec.bg)
        toast.attributes("-topmost", True)
        try:
            toast.attributes("-type", "notification")
        except tk.TclError:
            pass
        toast.resizable(True, True)
        label = tk.Label(
            toast,
            text=notification_message(message),
            bg=spec.bg,
            fg=spec.fg,
            font=font_spec(toast, 11, "bold"),
            justify="center",
            wraplength=270,
            padx=10,
            pady=8,
        )
        label.pack(expand=True, fill="both")
        toast.bind(
            "<Configure>",
            lambda event: label.configure(wraplength=max(100, int(event.width) - 24)),
            add="+",
        )
        def dismiss(_event) -> str:
            close_toast(toast)
            return "break"

        label.bind("<ButtonPress-1>", dismiss)
        toast.bind("<ButtonPress-1>", dismiss)
        toast.update_idletasks()
        active_toasts.append(toast)
        toast.deiconify()
        toast.lift()
        # Schedule from the withdrawn host so destroying the Toplevel cannot
        # accidentally discard the one hard lifetime bound.
        hide_after_id = root.after(
            notification_timeout_ms(timeout_ms),
            lambda: close_toast(toast, cancel_timer=False),
        )
        print(f"notification-presenter: toast shown timeout_ms={timeout_ms}", flush=True)

    def pump() -> None:
        while True:
            try:
                message, timeout_ms = events.get_nowait()
            except queue.Empty:
                break
            show(message, timeout_ms)
        root.after(100, pump)

    def on_event(msg: dict) -> None:
        payload = msg.get("payload", {})
        if not isinstance(payload, dict) or "message" not in payload:
            return
        timeout_ms = notification_timeout_ms(
            payload.get("timeout_ms"),
            os.environ.get("MSYS_NOTIFY_TIMEOUT_MS", str(DEFAULT_TOAST_TIMEOUT_MS)),
        )
        events.put((notification_message(payload["message"]), timeout_ms))

    threading.Thread(target=lambda: client.run(on_event=on_event), daemon=True).start()
    root.after(100, pump)
    root.mainloop()
    return 0


def build_navigation_bar(root, spec: RoleSpec, client: MsysClient) -> None:
    import tkinter as tk

    mode = os.environ.get("MSYS_NAV_MODE", "buttons")
    root.configure(bg=spec.bg)
    last_action_at = 0.0
    feedback_events: queue.SimpleQueue[NavigationFeedback] = queue.SimpleQueue()
    feedback_revisions: dict[str, int] = {}
    button_widgets: list[Any] = []
    button_by_action: dict[str, Any] = {}
    button_labels = {
        action: task_text(f"navigation.{action}")
        for action in NAVIGATION_ACTIONS
    }
    pill_surface: Any | None = None
    pill_item: Any | None = None
    pill_feedback_animation: Callable[[str], None] | None = None

    def feedback(item: NavigationFeedback) -> None:
        # Broker work happens on a worker. Tk mutation remains on its own
        # thread through the bounded queue pump below.
        feedback_events.put(item)

    def apply_feedback(item: NavigationFeedback) -> None:
        action = "back" if item.action == "close" else item.action
        feedback_key = "pill" if pill_surface is not None else action
        revision = feedback_revisions.get(feedback_key, 0) + 1
        feedback_revisions[feedback_key] = revision
        color = "#4f8f72" if item.ok else "#a63d47"
        delay_ms = 260 if item.ok else 950
        if item.detail:
            print(f"navigation feedback {action}: {item.detail}", flush=True)

        if pill_surface is not None and pill_item is not None:
            if pill_feedback_animation is not None:
                pill_feedback_animation(color)
            else:
                pill_surface.itemconfigure(pill_item, fill=color)
            return

        target = button_by_action.get(action)
        if target is None:
            return
        target.configure(
            bg=color,
            text=button_labels[action] if item.ok else f"{button_labels[action]} !",
        )

        def restore_button() -> None:
            if revision == feedback_revisions.get(feedback_key):
                target.configure(bg="#202833", text=button_labels[action], relief="raised")

        root.after(delay_ms, restore_button)

    def pump_feedback() -> None:
        while True:
            try:
                item = feedback_events.get_nowait()
            except queue.Empty:
                break
            apply_feedback(item)
        root.after(40, pump_feedback)

    def trigger(name: str, action) -> None:
        nonlocal last_action_at
        now = time.monotonic()
        if now - last_action_at < 0.25:
            return
        last_action_at = now
        print(f"navigation tap: {name}", flush=True)
        # Never block Tk's pointer dispatch on a broker/provider round trip.
        # This also keeps a single physical release from being re-dispatched
        # after a slow call has outlived the debounce window.
        start_background_action(f"navigation-{name}", action)

    def home() -> None:
        trigger("home", lambda: perform_navigation_action("home", "button", feedback=feedback))

    def apps() -> None:
        trigger("apps", lambda: perform_navigation_action("apps", "button", feedback=feedback))

    def apps_swipe() -> None:
        trigger("apps", lambda: perform_navigation_action("apps", "swipe", feedback=feedback))

    def back() -> None:
        trigger("back", lambda: perform_navigation_action("back", "button", feedback=feedback))

    def close() -> None:
        trigger("close", lambda: perform_navigation_action("close", "swipe", feedback=feedback))

    actions = {
        "back": back,
        "close": close,
        "home": home,
        "apps": apps,
    }

    def dispatch_at(x: int, y: int) -> None:
        width = max(root.winfo_width(), 1)
        height = max(root.winfo_height(), 1)
        action = navigation_action_at(x, y, width, height)
        axis = "vertical" if navigation_is_vertical(width, height) else "horizontal"
        print(
            f"navigation pointer x={x} y={y} size={width}x{height} axis={axis}",
            flush=True,
        )
        actions[action]()

    def bind_hot_zone(widget) -> None:
        def handle(event):
            for button in button_widgets:
                button.configure(bg="#202833", relief="raised")
            dispatch_at(
                int(event.x_root - root.winfo_rootx()),
                int(event.y_root - root.winfo_rooty()),
            )
            return "break"

        # Bind once on the toplevel and act on release.  Child events propagate
        # to this binding, so binding labels/frames too would execute one tap
        # multiple times.
        widget.bind("<ButtonRelease-1>", handle, add="+")

    if mode == "pill":
        surface = tk.Canvas(
            root,
            bg=spec.bg,
            highlightthickness=0,
            borderwidth=0,
            cursor="hand2",
        )
        pill_surface = surface
        surface.pack(expand=True, fill="both")
        pill = surface.create_line(
            0,
            0,
            1,
            0,
            fill="#e7ebf0",
            width=4,
            capstyle=tk.ROUND,
        )
        pill_item = pill

        gesture = PillGestureStateMachine()
        visual_distance = 0.0
        visual_progress = 0.0
        visual_revision = 0
        hold_revision = 0
        layout_edge: str | None = None

        def current_edge() -> str:
            return infer_navigation_edge(
                root.winfo_width(),
                root.winfo_height(),
                root_x=root.winfo_rootx(),
                root_y=root.winfo_rooty(),
                screen_width=root.winfo_screenwidth(),
                screen_height=root.winfo_screenheight(),
                preferred=os.environ.get("MSYS_NAV_EDGE"),
            )

        def draw_pill(
            distance: float | None = None,
            progress: float | None = None,
            color_override: str | None = None,
        ) -> None:
            nonlocal visual_distance, visual_progress
            if distance is not None:
                visual_distance = max(0.0, float(distance))
            if progress is not None:
                visual_progress = min(1.0, max(0.0, float(progress)))
            x1, y1, x2, y2, thickness, color = navigation_pill_motion_visual(
                root.winfo_width(),
                root.winfo_height(),
                current_edge(),
                visual_distance,
                visual_progress,
            )
            surface.coords(pill, x1, y1, x2, y2)
            surface.itemconfigure(
                pill,
                width=thickness,
                fill=color_override or color,
            )

        def animate_pill_to_rest(
            color: str | None = None,
            *,
            duration_ms: int = 150,
        ) -> None:
            """Ease the pill home on Tk's timer; never sleep in pointer dispatch."""

            nonlocal visual_revision
            visual_revision += 1
            revision = visual_revision
            start_distance = visual_distance
            start_progress = max(visual_progress, 0.28 if color else 0.0)
            frames = max(4, int(duration_ms) // 18)

            def frame(index: int) -> None:
                if revision != visual_revision:
                    return
                amount = min(1.0, index / frames)
                remaining = (1.0 - amount) ** 3
                frame_color = (
                    _blend_color(color, "#e7ebf0", amount)
                    if color is not None
                    else None
                )
                draw_pill(
                    start_distance * remaining,
                    start_progress * remaining,
                    frame_color,
                )
                if index < frames:
                    root.after(18, lambda: frame(index + 1))
                else:
                    draw_pill(0.0, 0.0)

            frame(0)

        pill_feedback_animation = lambda color: animate_pill_to_rest(
            color,
            duration_ms=240,
        )

        def present_gesture(update: PillGestureUpdate) -> None:
            nonlocal visual_revision
            visual_revision += 1
            draw_pill(update.inward_distance, update.progress)
            if update.action is None:
                return
            print(
                "navigation pill gesture "
                f"phase={update.phase} action={update.action} "
                f"distance={update.inward_distance} elapsed_ms={update.elapsed_ms}",
                flush=True,
            )
            if update.action == "apps" and update.phase == "triggered":
                apps_swipe()
            else:
                actions[update.action]()

        def cancel_gesture(*, animate: bool = True) -> None:
            nonlocal hold_revision
            if not gesture.active:
                return
            hold_revision += 1
            update = gesture.cancel(time.monotonic())
            present_gesture(update)
            if animate:
                animate_pill_to_rest(duration_ms=120)

        def layout_pill() -> None:
            nonlocal layout_edge
            edge = current_edge()
            if layout_edge is not None and layout_edge != edge:
                # A policy reflow/rotation cannot reinterpret an in-flight
                # bottom swipe as a side-edge swipe.
                cancel_gesture(animate=False)
                draw_pill(0.0, 0.0)
            layout_edge = edge
            draw_pill()

        def local_pointer(event) -> tuple[int, int]:
            return (
                int(event.x_root - root.winfo_rootx()),
                int(event.y_root - root.winfo_rooty()),
            )

        def hold_timeout(revision: int) -> None:
            if revision != hold_revision or not gesture.active:
                return
            present_gesture(gesture.hold(time.monotonic()))

        def pill_press(event) -> str:
            nonlocal hold_revision, visual_revision
            x, y = local_pointer(event)
            visual_revision += 1
            hold_revision += 1
            revision = hold_revision
            present_gesture(gesture.press(x, y, time.monotonic(), current_edge()))
            root.after(PILL_RECENTS_HOLD_MS, lambda: hold_timeout(revision))
            return "break"

        def pill_motion(event) -> str:
            if not gesture.active:
                return "break"
            x, y = local_pointer(event)
            present_gesture(gesture.move(x, y, time.monotonic()))
            return "break"

        def pill_release(event) -> str:
            nonlocal hold_revision
            x, y = local_pointer(event)
            fallback = navigation_action_at(
                x,
                y,
                max(root.winfo_width(), 1),
                max(root.winfo_height(), 1),
            )
            update = gesture.release(
                x,
                y,
                time.monotonic(),
                fallback_action=fallback,
            )
            hold_revision += 1
            present_gesture(update)
            animate_pill_to_rest(duration_ms=150)
            return "break"

        def pill_cancel(_event=None) -> str:
            cancel_gesture()
            return "break"

        root.bind("<Configure>", lambda _event: root.after_idle(layout_pill), add="+")
        root.bind("<Escape>", pill_cancel, add="+")
        root.after_idle(layout_pill)

        # Bind every actual Tk target. Returning break prevents the same child
        # event from reaching the Toplevel bindtag and firing a second action.
        for target in (root, surface):
            target.bind("<ButtonPress-1>", pill_press, add="+")
            target.bind("<B1-Motion>", pill_motion, add="+")
            target.bind("<ButtonRelease-1>", pill_release, add="+")
            target.bind("<<TouchCancel>>", pill_cancel, add="+")
        root.after(40, pump_feedback)
        return

    row = tk.Frame(root, bg=spec.bg)
    row.pack(expand=True, fill="both", padx=8, pady=5)
    button_actions = [
        ("back", back),
        ("home", home),
        ("apps", apps),
    ]
    for action_name, command in button_actions:
        button = tk.Label(
            row,
            text=button_labels[action_name],
            bg="#202833",
            fg="white",
            relief="raised",
            font=font_spec(root, 12, "bold"),
            cursor="hand2",
        )
        button.pack(side="left", expand=True, fill="both", padx=4)
        bind_navigation_button(button, command)
        button_widgets.append(button)
        button_by_action[action_name] = button

    layout_state: dict[str, bool | None] = {"vertical": None}

    def layout_buttons() -> None:
        vertical = navigation_is_vertical(root.winfo_width(), root.winfo_height())
        if layout_state["vertical"] == vertical:
            return
        layout_state["vertical"] = vertical
        row.pack_configure(
            padx=5 if vertical else 8,
            pady=8 if vertical else 5,
        )
        for button in button_widgets:
            button.pack_forget()
            button.pack(
                side="top" if vertical else "left",
                expand=True,
                fill="both",
                padx=3 if vertical else 4,
                pady=3 if vertical else 0,
            )

    root.bind("<Configure>", lambda _event: root.after_idle(layout_buttons), add="+")
    root.after_idle(layout_buttons)
    bind_hot_zone(root)
    root.after(40, pump_feedback)


def bind_system_chrome_notification_toggle(root, role_call=None) -> None:
    """Open notifications by gesture and expose one keyboard quick target.

    The release binding deliberately lives on the Toplevel. The CH347 input
    path can deliver a usable top-level release even when Tk did not see the
    matching Label press, which is the same constraint as the navigation bar.
    """

    state: dict[str, float | bool | None] = {
        "press_y": None,
        "press_x": None,
        "gesture_triggered": False,
        "last_action_at": 0.0,
    }
    call = role_call or MsysClient.public_call

    def call_notification_center() -> None:
        try:
            response = call(
                "role:notification-center",
                "toggle",
                {},
                timeout=7,
            )
            print(f"system-chrome notification-center response: {response}", flush=True)
        except Exception as exc:
            print(f"system-chrome notification-center failed: {exc}", flush=True)

    def call_input_method() -> None:
        try:
            response = call(
                "role:input-method",
                "toggle",
                {},
                timeout=7,
            )
            print(f"system-chrome input-method response: {response}", flush=True)
        except Exception as exc:
            print(f"system-chrome input-method failed: {exc}", flush=True)

    def trigger(action: str = "notifications") -> bool:
        now = time.monotonic()
        last_action_at = float(state["last_action_at"] or 0.0)
        if now - last_action_at < 0.35:
            return False
        state["last_action_at"] = now
        start_background_action(
            f"system-chrome-{action}",
            call_input_method if action == "input-method" else call_notification_center,
        )
        return True

    def keyboard_hit(event) -> bool:
        try:
            width = max(1, int(root.winfo_width()))
            x = int(getattr(event, "x"))
        except (AttributeError, TypeError, ValueError):
            return False
        return x >= max(0, width - 52)

    def on_press(event) -> None:
        state["press_y"] = float(event.y_root)
        state["press_x"] = float(getattr(event, "x", -1))
        state["gesture_triggered"] = False

    def on_motion(event) -> None:
        press_y = state["press_y"]
        if press_y is None or state["gesture_triggered"]:
            return
        if float(event.y_root) - float(press_y) >= 18 and trigger():
            state["gesture_triggered"] = True

    def on_release(event) -> str:
        # A regular click toggles here. A downward drag normally toggles from
        # motion, while release-only input still reaches this fallback.
        if not state["gesture_triggered"]:
            trigger("input-method" if keyboard_hit(event) else "notifications")
        state["press_y"] = None
        state["press_x"] = None
        state["gesture_triggered"] = False
        return "break"

    root.bind("<ButtonPress-1>", on_press, add="+")
    root.bind("<B1-Motion>", on_motion, add="+")
    root.bind("<ButtonRelease-1>", on_release, add="+")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=sorted(ROLES))
    args = parser.parse_args(argv)

    if os.environ.get("MSYS_ROLE_HEADLESS") == "1":
        return run_headless(args.role)
    # Preserve the historical visual CLI while routing the canonical
    # screen-shield role to its typed provider. Explicit headless diagnostics
    # above retain their old no-X behavior.
    if args.role == "screen-shield":
        from .screen_shield import main as screen_shield_main

        return screen_shield_main()
    try:
        return run_tk(args.role, visible=os.environ.get("MSYS_ROLE_VISIBLE", "1") != "0")
    except Exception as exc:
        print(f"{args.role} tk failed: {exc}", flush=True)
        # A visual role that cannot reach X must fail so msysd can restart or
        # quarantine it.  Silent headless success leaves the screen without
        # navigation while reporting a misleading ready state.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
