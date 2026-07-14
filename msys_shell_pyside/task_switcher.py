from __future__ import annotations

import os
import queue
import threading
from dataclasses import dataclass
from typing import Any

from msys_sdk import MsysClient

from .adaptive import adaptive_panel_geometry
from .localization import SHELL_I18N, shell_text
from msys_sdk.ui_fonts import configure_tk_fonts, font_spec


# Keep visible copy at one seam so the shared SDK translator can replace this
# lookup without spreading locale conditionals through Tk layout code.
TASK_SWITCHER_COPY = {
    "title": "Recent tasks",
    "dismiss": "Close",
    "summary.one": "1 recent task",
    "summary.many": "{count} recent tasks",
    "empty.title": "Nothing recent",
    "empty.detail": "Apps you open will appear here.",
    "action.open": "Open",
    "action.close": "Close",
    "action.opening": "Opening {title}...",
    "action.closing": "Closing {title}...",
    "action.failed": "Action failed",
    "more": "+{count} more",
    "status.active": "Active",
    "status.running": "Running",
    "status.background": "In background",
    "status.attention": "Needs attention",
    "status.external": "External window",
    "application": "Application",
    "navigation.back": "Back",
    "navigation.home": "Home",
    "navigation.apps": "Apps",
}
TASK_SWITCHER_I18N = SHELL_I18N


def task_text(key: str, **values: object) -> str:
    normalized_key = str(key)
    template = TASK_SWITCHER_COPY.get(normalized_key, normalized_key)
    catalog_key = (
        normalized_key
        if normalized_key.startswith("navigation.")
        else "task_switcher." + normalized_key
    )
    return shell_text(
        catalog_key,
        values,
        fallback=template,
    )


@dataclass(frozen=True, slots=True)
class RecentStatus:
    label: str
    tone: str


def recent_status(item: dict[str, Any]) -> RecentStatus:
    """Present useful state without inventing lifecycle controls for X11 rows."""

    if item.get("active") is True or item.get("focused") is True:
        return RecentStatus(task_text("status.active"), "active")
    state = str(
        item.get("component_state")
        or item.get("state")
        or item.get("window_state")
        or ""
    ).strip().lower()
    if state in {"failed", "crashed", "error", "quarantined"}:
        return RecentStatus(task_text("status.attention"), "warning")
    if state in {"minimized", "hidden", "suspended", "background"}:
        return RecentStatus(task_text("status.background"), "muted")
    if str(item.get("component") or "").strip():
        return RecentStatus(task_text("status.running"), "running")
    return RecentStatus(task_text("status.external"), "muted")


@dataclass(frozen=True, slots=True)
class PanelMotionFrame:
    alpha: float
    offset: int


def panel_motion_frames(
    appearing: bool,
    *,
    steps: int = 7,
    travel: int = 12,
) -> tuple[PanelMotionFrame, ...]:
    """Return a short cubic fade/slide sequence for Tk ``after`` callbacks."""

    frame_count = max(2, int(steps))
    distance = max(0, int(travel))
    frames: list[PanelMotionFrame] = []
    for index in range(frame_count + 1):
        position = index / frame_count
        eased = 1.0 - (1.0 - position) ** 3
        if appearing:
            alpha = 0.18 + 0.82 * eased
            offset = round(distance * (1.0 - eased))
        else:
            alpha = max(0.05, 1.0 - 0.95 * eased)
            offset = round(distance * eased)
        frames.append(PanelMotionFrame(alpha=alpha, offset=offset))
    return tuple(frames)


def window_manager(method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return MsysClient.public_call(
        "role:window-manager",
        method,
        payload or {},
        timeout=7,
    )


def return_payload(response: object, operation: str) -> dict[str, Any]:
    """Return one successful mIPC payload or raise a visible action error."""

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
    return dict(payload)


def recent_windows(
    *,
    public_call: Any | None = None,
) -> list[dict[str, Any]]:
    """Fetch a bounded, actionable recent-window snapshot from window policy."""

    caller = public_call or MsysClient.public_call
    payload = return_payload(
        caller("role:window-manager", "recents", {}, timeout=7),
        "window-manager.recents",
    )
    raw_windows = payload.get("windows", [])
    if not isinstance(raw_windows, list):
        raise RuntimeError("window-manager.recents returned an invalid window list")
    windows: list[dict[str, Any]] = []
    for raw in raw_windows[:32]:
        if not isinstance(raw, dict):
            continue
        component = str(raw.get("component") or "").strip()
        window_id = str(raw.get("id") or "").strip()
        if not component and not window_id:
            continue
        title = str(
            raw.get("title") or component or window_id or task_text("application")
        )[:256]
        windows.append({
            **dict(raw),
            "component": component,
            "id": window_id,
            "title": title,
        })
    return windows


def activate_recent(
    item: dict[str, Any],
    *,
    public_call: Any | None = None,
) -> dict[str, Any]:
    """Reactivate a managed recent through Core's lifecycle/window transaction."""

    component = str(item.get("component") or "").strip()
    if not component or ":" not in component:
        raise ValueError("recent entry is not an actionable managed component")
    caller = public_call or MsysClient.public_call
    payload = return_payload(
        caller("msys.core", "start", {"component": component}, timeout=8),
        f"start {component}",
    )
    activation_error = payload.get("activation_error")
    if isinstance(activation_error, dict):
        detail = activation_error.get("message") or activation_error.get("code") or "window activation failed"
        raise RuntimeError(f"activate {component} failed: {detail}")
    activation = payload.get("activation")
    if isinstance(activation, dict) and activation.get("ok") is False:
        detail = activation.get("reason") or activation.get("stderr") or "window activation failed"
        raise RuntimeError(f"activate {component} failed: {detail}")
    return payload


def close_recent(
    item: dict[str, Any],
    *,
    public_call: Any | None = None,
) -> dict[str, Any]:
    """Make the selected managed task active, then let window policy close it."""

    caller = public_call or MsysClient.public_call
    activate_recent(item, public_call=caller)
    payload = return_payload(
        caller("role:window-manager", "close_active", {}, timeout=8),
        "window-manager.close_active",
    )
    if payload.get("ok") is False:
        detail = payload.get("reason") or payload.get("error") or "close was rejected"
        raise RuntimeError(f"window-manager.close_active failed: {detail}")
    return payload


def main() -> int:
    import tkinter as tk

    root = tk.Tk(className=os.environ.get("MSYS_WINDOW_IDENTITY", "MsysTaskSwitcher"))
    configure_tk_fonts(root, default_size=10)
    root.title("msys-task-switcher-host")
    root.withdraw()
    root.update_idletasks()

    client = MsysClient.from_env()
    client.hello()
    client.ready()
    client.event("msys.role.ready", {"role": "task-switcher", "component": client.component_id})

    actions: queue.Queue[tuple[str, Any]] = queue.Queue()
    panel: tk.Toplevel | None = None
    panel_status: Any | None = None
    panel_buttons: list[Any] = []
    panel_visible = threading.Event()
    panel_revision = 0

    def destroy_panel(target: Any) -> None:
        nonlocal panel, panel_status
        try:
            target.destroy()
        except tk.TclError:
            pass
        if panel is target:
            panel = None
            panel_status = None
            panel_buttons.clear()

    def animate_panel(
        target: Any,
        appearing: bool,
        *,
        on_complete: Any | None = None,
    ) -> None:
        """Fade and nudge one panel using only short Tk timer callbacks."""

        nonlocal panel_revision
        panel_revision += 1
        revision = panel_revision
        try:
            target.update_idletasks()
            final_x = int(target.winfo_x())
            final_y = int(target.winfo_y())
        except tk.TclError:
            return
        frames = panel_motion_frames(appearing)

        def step(index: int) -> None:
            if revision != panel_revision or panel is not target:
                return
            try:
                frame = frames[index]
                target.geometry(f"+{final_x}+{final_y + frame.offset}")
                try:
                    target.attributes("-alpha", frame.alpha)
                except tk.TclError:
                    # Alpha is optional on tiny X servers; the slide remains.
                    pass
            except tk.TclError:
                return
            if index + 1 < len(frames):
                root.after(18, lambda: step(index + 1))
                return
            if appearing:
                try:
                    target.geometry(f"+{final_x}+{final_y}")
                    target.attributes("-alpha", 1.0)
                except tk.TclError:
                    pass
            if on_complete is not None:
                on_complete()

        step(0)

    def hide_panel(*, animate: bool = True) -> None:
        nonlocal panel_revision
        target = panel
        panel_visible.clear()
        if target is None:
            return
        for button in panel_buttons:
            try:
                button.configure(state="disabled")
            except tk.TclError:
                pass
        if animate:
            animate_panel(
                target,
                False,
                on_complete=lambda: destroy_panel(target),
            )
        else:
            panel_revision += 1
            destroy_panel(target)

    def run_control(action: str, item: dict[str, Any]) -> None:
        try:
            if action == "open":
                result = activate_recent(item)
            elif action == "close":
                result = close_recent(item)
            else:
                raise ValueError(f"unknown recent action {action}")
            actions.put(("control-result", {
                "ok": True,
                "action": action,
                "component": item.get("component"),
                "result": result,
            }))
        except Exception as exc:
            print(f"task-switcher: {action} failed: {exc}", flush=True)
            actions.put(("control-result", {
                "ok": False,
                "action": action,
                "component": item.get("component"),
                "error": str(exc),
            }))

    def start_control(action: str, item: dict[str, Any]) -> None:
        if panel_status is not None:
            title = str(
                item.get("title")
                or item.get("component")
                or task_text("application")
            )
            copy_key = "action.opening" if action == "open" else "action.closing"
            panel_status.set(task_text(copy_key, title=title))
        for button in panel_buttons:
            button.configure(state="disabled")
        if action == "close" and panel is not None:
            # Back is specified to dismiss the recents overlay first. Hide it
            # before close_active so window policy sees the selected app, not
            # this task-switcher panel, as the top dismissible surface.
            panel.withdraw()
            panel_visible.clear()
        threading.Thread(
            target=run_control,
            args=(action, dict(item)),
            name=f"msys-task-switcher-{action}",
            daemon=True,
        ).start()

    def show_panel(windows: list[dict[str, Any]]) -> None:
        nonlocal panel, panel_status
        hide_panel(animate=False)
        panel = tk.Toplevel(
            root,
            class_=os.environ.get("MSYS_WINDOW_IDENTITY", "MsysTaskSwitcher"),
        )
        panel.withdraw()
        panel.title(task_text("title"))
        panel.geometry(adaptive_panel_geometry(
            root,
            width_ratio=0.92,
            height_ratio=0.72,
            minimum_width=250,
            minimum_height=240,
            maximum_width=540,
            maximum_height=700,
        ))
        panel.configure(bg="#11151b")
        panel.attributes("-topmost", True)
        panel.resizable(True, True)
        panel.minsize(220, 180)

        header = tk.Frame(panel, bg="#11151b")
        header.pack(fill="x", padx=14, pady=(12, 6))
        tk.Frame(header, bg="#82b1ff", width=4, height=28).pack(
            side="left", padx=(0, 9)
        )
        tk.Label(
            header,
            text=task_text("title"),
            bg="#11151b",
            fg="#f2f5f8",
            font=font_spec(panel, 15, "bold"),
            anchor="w",
        ).pack(side="left", expand=True, fill="x")
        tk.Button(
            header,
            text=task_text("dismiss"),
            command=hide_panel,
            bg="#252c35",
            fg="#e7ebf0",
            activebackground="#37414d",
            activeforeground="white",
            relief="flat",
            borderwidth=0,
            padx=9,
            pady=5,
            cursor="hand2",
        ).pack(side="right")
        summary_key = "summary.one" if len(windows) == 1 else "summary.many"
        panel_status = tk.StringVar(
            value=task_text(summary_key, count=len(windows))
        )
        tk.Label(
            panel,
            textvariable=panel_status,
            bg="#11151b",
            fg="#9ba8b6",
            anchor="w",
            font=font_spec(panel, 9),
        ).pack(fill="x", padx=14, pady=(0, 5))

        if not windows:
            empty = tk.Frame(
                panel,
                bg="#1a2028",
                highlightbackground="#2d3641",
                highlightthickness=1,
            )
            empty.pack(expand=True, fill="both", padx=14, pady=(8, 14))
            tk.Label(
                empty,
                text="--",
                bg="#1a2028",
                fg="#82b1ff",
                font=font_spec(panel, 22, "bold"),
            ).pack(pady=(28, 3))
            tk.Label(
                empty,
                text=task_text("empty.title"),
                bg="#1a2028",
                fg="#f2f5f8",
                font=font_spec(panel, 12, "bold"),
            ).pack()
            tk.Label(
                empty,
                text=task_text("empty.detail"),
                bg="#1a2028",
                fg="#9ba8b6",
                font=font_spec(panel, 9),
            ).pack(pady=(4, 28))
        card_limit = 3 if root.winfo_screenheight() < 560 else 4
        for item in windows[:card_limit]:
            component = str(item.get("component", ""))
            title = str(item.get("title") or component or task_text("application"))
            status = recent_status(item)
            tone_colors = {
                "active": ("#153a35", "#80cbc4"),
                "running": ("#18324e", "#9cc9ff"),
                "warning": ("#4b292d", "#ffb4ab"),
                "muted": ("#303740", "#bdc7d2"),
            }
            chip_bg, chip_fg = tone_colors[status.tone]
            card = tk.Frame(
                panel,
                bg="#202731",
                highlightbackground="#343e4a",
                highlightthickness=1,
                padx=9,
                pady=7,
            )
            card.pack(fill="x", padx=14, pady=4)
            card_header = tk.Frame(card, bg="#202731")
            card_header.pack(fill="x")
            tk.Label(
                card_header,
                text=title,
                bg="#202731",
                fg="#f4f6f8",
                anchor="w",
                font=font_spec(panel, 11, "bold"),
            ).pack(side="left", expand=True, fill="x")
            tk.Label(
                card_header,
                text=status.label,
                bg=chip_bg,
                fg=chip_fg,
                padx=6,
                pady=2,
                font=font_spec(panel, 8, "bold"),
            ).pack(side="right", padx=(6, 0))
            identity = str(
                item.get("identity")
                or component
                or item.get("source")
                or item.get("id")
                or "X11"
            )[:72]
            tk.Label(
                card,
                text=identity,
                bg="#202731",
                fg="#98a5b3",
                anchor="w",
                font=font_spec(panel, 8),
            ).pack(fill="x", pady=(3, 4))
            if component:
                controls = tk.Frame(card, bg="#202731")
                controls.pack(fill="x")
                open_button = tk.Button(
                    controls,
                    text=task_text("action.open"),
                    command=lambda value=dict(item): start_control("open", value),
                    bg="#82b1ff",
                    fg="#0c1b2b",
                    activebackground="#a9caff",
                    activeforeground="#0c1b2b",
                    relief="flat",
                    borderwidth=0,
                    padx=11,
                    pady=3,
                    cursor="hand2",
                )
                close_button = tk.Button(
                    controls,
                    text=task_text("action.close"),
                    command=lambda value=dict(item): start_control("close", value),
                    bg="#343d48",
                    fg="#eef2f6",
                    activebackground="#485462",
                    activeforeground="white",
                    relief="flat",
                    borderwidth=0,
                    padx=11,
                    pady=3,
                    cursor="hand2",
                )
                close_button.pack(side="right", padx=(5, 0))
                open_button.pack(side="right")
                panel_buttons.extend((open_button, close_button))

        if len(windows) > card_limit:
            tk.Label(
                panel,
                text=task_text("more", count=len(windows) - card_limit),
                bg="#11151b",
                fg="#9ba8b6",
                font=font_spec(panel, 9),
            ).pack(pady=(3, 7))

        panel.update_idletasks()
        panel.deiconify()
        panel.lift()
        panel_visible.set()
        animate_panel(panel, True)

    def pump() -> None:
        while True:
            try:
                action, payload = actions.get_nowait()
            except queue.Empty:
                break
            if action == "show":
                show_panel(list(payload))
            elif action == "hide":
                hide_panel()
            elif action == "control-result":
                result = dict(payload)
                if result.get("ok"):
                    hide_panel(animate=result.get("action") == "open")
                else:
                    if panel_status is not None:
                        panel_status.set(
                            str(result.get("error") or task_text("action.failed"))
                        )
                    for button in panel_buttons:
                        button.configure(state="normal")
                    if panel is not None:
                        panel.deiconify()
                        panel.lift()
                        panel_visible.set()
                        animate_panel(panel, True)
            elif action == "shutdown":
                hide_panel(animate=False)
                root.destroy()
                return
        root.after(50, pump)

    def ipc_loop() -> None:
        while True:
            message = client.recv(timeout=None)
            if not message or message.get("type") in {"eof", "shutdown"}:
                actions.put(("shutdown", None))
                return
            if message.get("type") != "call":
                continue
            request_id = int(message.get("id", 0))
            method = str(message.get("method", ""))
            try:
                if method in {"show", "toggle"}:
                    if method == "toggle" and panel_visible.is_set():
                        actions.put(("hide", None))
                        payload = {"ok": True, "visible": False, "count": 0}
                    else:
                        windows = recent_windows()
                        actions.put(("show", windows))
                        payload = {"ok": True, "visible": True, "count": len(windows)}
                elif method == "hide":
                    actions.put(("hide", None))
                    payload = {"ok": True, "visible": False}
                else:
                    raise ValueError(f"unknown method {method}")
                client.send({"type": "return", "id": request_id, "payload": payload})
            except Exception as exc:
                client.send({
                    "type": "error",
                    "id": request_id,
                    "code": "TASK_SWITCHER_ERROR",
                    "message": str(exc),
                })

    threading.Thread(target=ipc_loop, name="msys-task-switcher-ipc", daemon=True).start()
    root.after(50, pump)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
