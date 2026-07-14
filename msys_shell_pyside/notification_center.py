from __future__ import annotations

import time

# Capture this before importing the SDK and the rest of the provider so the
# opt-in startup trace includes Python/module loading, not just Tk work.
_MODULE_ENTRY_STARTED = time.perf_counter()

import json
import os
import queue
import secrets
import tempfile
import textwrap
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from msys_sdk import MsysClient

from .adaptive import UiRect, adaptive_panel_rect
from .localization import shell_text
from msys_sdk.ui_fonts import (
    NAMED_TK_FONTS,
    configure_tk_fonts,
    font_spec,
    logical_size_to_pixels,
    requested_font_family,
)


HISTORY_SCHEMA = "msys.notification-history.v1"
NOTIFICATION_TOPICS = frozenset({
    "msys.role.notification-presenter",
    "msys.notification.post",
})
DEFAULT_HISTORY_LIMIT = 100
MAX_HISTORY_LIMIT = 1000
MAX_TITLE_CHARS = 256
MAX_MESSAGE_CHARS = 4096
MAX_SOURCE_CHARS = 256
MAX_TOPIC_CHARS = 128
MAX_URGENCY_CHARS = 32
MIN_NOTIFICATION_WRAP_CHARS = 12
MAX_NOTIFICATION_WRAP_CHARS = 80
ESTIMATED_GLYPH_PIXELS = 13
INITIAL_RENDER_NOTIFICATIONS = 16
HOST_PUMP_INTERVAL_MS = 40
NOTIFICATION_APP_ID = "org.msys.shell.pyside"
NOTIFICATION_COMPONENT_ID = "org.msys.shell.pyside:notification-center"
NOTIFICATION_WINDOW_IDENTITY = "org.msys.shell.notification-center"

# Shared light Material-ish palette. The panel deliberately uses flat Tk
# primitives so it remains available on the 11 MiB target without ttk themes
# or image assets.
WINDOW_BG = "#F4F7FB"
SURFACE = "#FFFFFF"
TEXT_PRIMARY = "#172033"
PRIMARY = "#2563EB"
PRIMARY_CONTAINER = "#E8F0FE"
OUTLINE = "#D6DEE9"
SELECTED = "#DCE8FF"


def configure_notification_fonts(
    root: Any,
    *,
    default_size: int = 10,
    font_module: Any | None = None,
) -> str | None:
    """Use an explicit supervised family without enumerating every Xft font.

    Automatic family selection retains the SDK's CJK-capable policy. When the
    profile already supplies MSYS_UI_FONT_FAMILY, enumerating the complete
    target font set is redundant and can dominate cold starts on small boards.
    """

    family = requested_font_family()
    if not family:
        return configure_tk_fonts(root, default_size=default_size)
    from tkinter import TclError

    if font_module is None:
        from tkinter import font as font_module

    options = {
        "family": family,
        "size": -logical_size_to_pixels(default_size),
    }
    for name in NAMED_TK_FONTS:
        try:
            font_module.nametofont(name, root=root).configure(**options)
        except (TclError, RuntimeError, TypeError):
            continue
    setattr(root, "_msys_tk_font_family", family)
    return family


def configure_notification_window_identity(
    window: Any,
    *,
    environ: Mapping[str, str] | None = None,
) -> Any:
    """Apply the notification role identity to each managed Tk surface.

    The package manifest historically used the window class as ``MSYS_APP_ID``.
    Do not let that legacy value leak into the canonical package/component
    properties on the independently managed notification ``Toplevel``.
    """

    from msys_sdk.ui_identity import configure_tk_window_identity

    values = dict(os.environ if environ is None else environ)
    values["MSYS_APP_ID"] = NOTIFICATION_APP_ID
    values["MSYS_COMPONENT_ID"] = NOTIFICATION_COMPONENT_ID
    values["MSYS_WINDOW_ROLE"] = "notification-center"
    values["MSYS_WINDOW_IDENTITY"] = NOTIFICATION_WINDOW_IDENTITY
    return configure_tk_window_identity(
        window,
        NOTIFICATION_APP_ID,
        default_role="notification-center",
        default_instance="notification-center",
        environ=values,
    )


def startup_timing_enabled(environ: Mapping[str, str] | None = None) -> bool:
    values = os.environ if environ is None else environ
    return str(
        values.get("MSYS_STARTUP_TIMING", values.get("DEBUG", ""))
    ).strip().casefold() in {"1", "true", "yes", "on"}


def _bounded_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (int, float, bool)):
        text = str(value)
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "\N{HORIZONTAL ELLIPSIS}"


def _coerce_timestamp_ms(value: Any, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        timestamp = int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return timestamp if timestamp >= 0 else fallback


def notification_wrap_limit(pixel_width: int) -> int:
    """Return a conservative line width for Latin and full-width CJK copy."""

    usable = max(1, int(pixel_width) - 18)
    return min(
        MAX_NOTIFICATION_WRAP_CHARS,
        max(MIN_NOTIFICATION_WRAP_CHARS, usable // ESTIMATED_GLYPH_PIXELS),
    )


def notification_lines(
    notifications: list[dict[str, Any]],
    *,
    character_limit: int,
) -> list[str]:
    """Format and wrap history rows without depending on a Tk widget."""

    if not notifications:
        return [shell_text("notification.empty")]
    width = min(
        MAX_NOTIFICATION_WRAP_CHARS,
        max(MIN_NOTIFICATION_WRAP_CHARS, int(character_limit)),
    )
    lines: list[str] = []
    for item in notifications:
        stamp = time.strftime(
            "%H:%M",
            time.localtime(int(item.get("timestamp_ms", 0)) / 1000),
        )
        title = str(item.get("title", "")).strip()
        message = str(item.get("message", "")).replace("\n", " ").strip()
        source = str(item.get("source", "")).strip()
        shown = message or shell_text("notification.fallback")
        line = shell_text(
            "notification.entry.title" if title else "notification.entry.message",
            time=stamp,
            title=title,
            message=shown,
        )
        if source:
            line = shell_text("notification.entry.source", line=line, source=source)
        wrapped = textwrap.wrap(
            line,
            width=width,
            replace_whitespace=True,
            drop_whitespace=True,
            break_long_words=True,
            break_on_hyphens=False,
        )
        lines.extend(wrapped or [line])
    return lines


def normalize_notification(
    topic: str,
    payload: Any,
    source: str = "",
    *,
    timestamp_ms: int | None = None,
    notification_id: str | None = None,
) -> dict[str, Any]:
    """Convert an untrusted broadcast payload to the bounded history shape."""

    now_ms = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
    raw = payload if isinstance(payload, dict) else {"message": payload}
    title = raw.get("title", raw.get("summary", ""))
    message = raw.get("message", raw.get("body", raw.get("text", "")))
    if not message and title:
        message = title
        title = ""
    payload_source = raw.get("source", raw.get("application", raw.get("app", "")))
    entry_source = source or _bounded_text(payload_source, MAX_SOURCE_CHARS)
    entry_id = notification_id or f"{now_ms:x}-{secrets.token_hex(4)}"
    return {
        "id": _bounded_text(entry_id, 96),
        "timestamp_ms": max(0, now_ms),
        "topic": _bounded_text(topic, MAX_TOPIC_CHARS),
        "source": _bounded_text(entry_source, MAX_SOURCE_CHARS),
        "title": _bounded_text(title, MAX_TITLE_CHARS),
        "message": _bounded_text(message, MAX_MESSAGE_CHARS),
        "urgency": _bounded_text(raw.get("urgency", "normal"), MAX_URGENCY_CHARS) or "normal",
    }


def _normalize_stored_entry(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    entry_id = _bounded_text(raw.get("id", ""), 96)
    if not entry_id:
        return None
    timestamp_ms = _coerce_timestamp_ms(raw.get("timestamp_ms"), 0)
    return normalize_notification(
        _bounded_text(raw.get("topic", ""), MAX_TOPIC_CHARS),
        {
            "title": raw.get("title", ""),
            "message": raw.get("message", ""),
            "urgency": raw.get("urgency", "normal"),
        },
        _bounded_text(raw.get("source", ""), MAX_SOURCE_CHARS),
        timestamp_ms=timestamp_ms,
        notification_id=entry_id,
    )


class NotificationHistoryStore:
    """Provider-owned, bounded notification history with atomic replacement."""

    def __init__(self, path: Path, limit: int = DEFAULT_HISTORY_LIMIT) -> None:
        self.path = path
        self.limit = min(MAX_HISTORY_LIMIT, max(1, int(limit)))
        self._lock = threading.RLock()
        self._items, needs_rewrite = self._read()
        if needs_rewrite:
            self._write(self._items)

    def _read(self) -> tuple[list[dict[str, Any]], bool]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return [], False
        if not isinstance(raw, dict) or raw.get("schema") != HISTORY_SCHEMA:
            return [], False
        values = raw.get("notifications", [])
        if not isinstance(values, list):
            return [], True
        items = [item for value in values if (item := _normalize_stored_entry(value)) is not None]
        trimmed = items[-self.limit :]
        return trimmed, len(trimmed) != len(values) or trimmed != values

    def _write(self, items: list[dict[str, Any]]) -> bool:
        directory = self.path.parent
        temporary: str | None = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=directory)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                    json.dump(
                        {"schema": HISTORY_SCHEMA, "notifications": items},
                        stream,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
            os.replace(temporary, self.path)
            temporary = None
            try:
                directory_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
            return True
        except OSError as exc:
            print(f"notification-center: cannot save history: {exc}", flush=True)
            return False
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def append(self, item: dict[str, Any]) -> dict[str, Any]:
        normalized = _normalize_stored_entry(item)
        if normalized is None:
            raise ValueError("notification requires a non-empty id")
        with self._lock:
            self._items.append(normalized)
            del self._items[: max(0, len(self._items) - self.limit)]
            self._write(self._items)
            return dict(normalized)

    def list(self, limit: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            count = self.limit if limit is None else min(self.limit, max(0, int(limit)))
            # The persisted representation is chronological. Consumers and the
            # panel receive the newest notification first.
            return [dict(item) for item in reversed(self._items[-count:])] if count else []

    def count(self) -> int:
        with self._lock:
            return len(self._items)

    def clear(self) -> int:
        with self._lock:
            removed = len(self._items)
            self._items.clear()
            self._write(self._items)
            return removed


def history_path_from_env() -> Path:
    explicit = os.environ.get("MSYS_NOTIFICATION_HISTORY")
    if explicit:
        return Path(explicit)
    state_dir = Path(os.environ.get("MSYS_STATE_DIR", "/opt/msys-state"))
    return state_dir / "notifications" / "history.json"


def history_limit_from_env() -> int:
    try:
        value = int(os.environ.get("MSYS_NOTIFICATION_HISTORY_LIMIT", str(DEFAULT_HISTORY_LIMIT)))
    except ValueError:
        value = DEFAULT_HISTORY_LIMIT
    return min(MAX_HISTORY_LIMIT, max(1, value))


class NotificationCenterService:
    """mIPC/event behavior kept independent of Tk for headless testing."""

    def __init__(
        self,
        store: NotificationHistoryStore,
        actions: queue.Queue[tuple[str, Any]],
        *,
        id_factory: Callable[[], str] | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.store = store
        self.actions = actions
        self.id_factory = id_factory
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._lock = threading.RLock()
        self._visible = False

    @property
    def visible(self) -> bool:
        with self._lock:
            return self._visible

    def set_visible(self, visible: bool, *, notify_ui: bool = True) -> bool:
        with self._lock:
            self._visible = bool(visible)
            current = self._visible
        if notify_ui:
            self.actions.put(("visibility", current))
        return current

    def handle_event(self, message: dict[str, Any]) -> dict[str, Any] | None:
        topic = str(message.get("topic", ""))
        if topic not in NOTIFICATION_TOPICS:
            return None
        notification_id = self.id_factory() if self.id_factory is not None else None
        item = normalize_notification(
            topic,
            message.get("payload", {}),
            str(message.get("source", "")),
            timestamp_ms=self.clock_ms(),
            notification_id=notification_id,
        )
        stored = self.store.append(item)
        self.actions.put(("history", self.store.list()))
        print(
            f"notification-center: stored id={stored['id']} topic={stored['topic']}",
            flush=True,
        )
        return stored

    def handle_call(self, message: dict[str, Any]) -> dict[str, Any]:
        request_id = int(message.get("id", 0))
        method = str(message.get("method", ""))
        payload = message.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}

        if method == "show":
            visible = self.set_visible(True)
            return self._return(request_id, {"visible": visible, "count": self.store.count()})
        if method == "hide":
            visible = self.set_visible(False)
            return self._return(request_id, {"visible": visible, "count": self.store.count()})
        if method == "toggle":
            visible = self.set_visible(not self.visible)
            return self._return(request_id, {"visible": visible, "count": self.store.count()})
        if method == "list":
            requested_limit = payload.get("limit")
            try:
                limit = None if requested_limit is None else int(requested_limit)
            except (TypeError, ValueError, OverflowError):
                return self._error(request_id, "BAD_REQUEST", "limit must be an integer")
            notifications = self.store.list(limit)
            return self._return(request_id, {
                "notifications": notifications,
                "count": self.store.count(),
                "limit": self.store.limit,
                "visible": self.visible,
            })
        if method == "clear":
            removed = self.store.clear()
            self.actions.put(("history", []))
            return self._return(request_id, {
                "removed": removed,
                "count": 0,
                "visible": self.visible,
            })
        return self._error(request_id, "NO_METHOD", method)

    @staticmethod
    def _return(request_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return {"type": "return", "id": request_id, "payload": payload}

    @staticmethod
    def _error(request_id: int, code: str, message: str) -> dict[str, Any]:
        return {"type": "error", "id": request_id, "code": code, "message": message}


class NotificationCenterUi:
    def __init__(
        self,
        root: Any,
        service: NotificationCenterService,
        on_clear: Callable[[], None],
    ) -> None:
        self.root = root
        self.service = service
        self.on_clear = on_clear
        self.panel: Any | None = None
        self.listbox: Any | None = None
        self.count_label: Any | None = None
        self.close_zone: Any | None = None
        self.clear_zone: Any | None = None
        self._notifications: list[dict[str, Any]] = []
        self._wrap_limit = 0
        self._render_revision = 0

    def _create_panel(self) -> None:
        import tkinter as tk

        if self.panel is not None:
            return
        panel = tk.Toplevel(
            self.root,
            class_=os.environ.get("MSYS_WINDOW_IDENTITY", "MsysNotificationCenter"),
        )
        self.panel = panel
        panel.withdraw()
        # A Toplevel has its own X11 wrapper and does not inherit the hidden
        # host's properties.  Apply identity after native creation/withdrawal
        # and before its first MapRequest.
        configure_notification_window_identity(panel)
        panel.title(shell_text("notification.window_title"))
        panel.geometry(self._geometry())
        panel.configure(bg=WINDOW_BG)
        panel.attributes("-topmost", True)
        panel.resizable(True, True)
        panel.minsize(220, 180)
        panel.protocol("WM_DELETE_WINDOW", self.hide_from_ui)

        header = tk.Frame(panel, bg=SURFACE, highlightthickness=1, highlightbackground=OUTLINE)
        header.pack(fill="x", padx=8, pady=(8, 6))
        tk.Label(
            header,
            text=shell_text("notification.title"),
            bg=SURFACE,
            fg=TEXT_PRIMARY,
            font=font_spec(panel, 13, "bold"),
            anchor="w",
        ).pack(side="left", expand=True, fill="x", padx=(10, 4), pady=8)
        self.count_label = tk.Label(
            header,
            text="0",
            bg=PRIMARY_CONTAINER,
            fg=PRIMARY,
            font=font_spec(panel, 9),
            padx=7,
            pady=4,
        )
        self.count_label.pack(side="left", padx=5)
        self.clear_zone = tk.Label(
            header,
            text=shell_text("notification.clear"),
            bg=PRIMARY_CONTAINER,
            fg=PRIMARY,
            padx=8,
            pady=7,
            cursor="hand2",
        )
        self.clear_zone.pack(side="left", padx=2)
        self.close_zone = tk.Label(
            header,
            text=shell_text("notification.close"),
            bg=PRIMARY,
            fg=SURFACE,
            padx=8,
            pady=7,
            cursor="hand2",
        )
        self.close_zone.pack(side="left", padx=(2, 8))

        frame = tk.Frame(
            panel,
            bg=SURFACE,
            highlightthickness=1,
            highlightbackground=OUTLINE,
        )
        frame.pack(expand=True, fill="both", padx=8, pady=(0, 8))
        self.listbox = tk.Listbox(
            frame,
            bg=SURFACE,
            fg=TEXT_PRIMARY,
            selectbackground=SELECTED,
            selectforeground=TEXT_PRIMARY,
            activestyle="none",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            font=font_spec(panel, 10),
        )
        self.listbox.pack(side="left", expand=True, fill="both", padx=(8, 2), pady=8)
        scrollbar = tk.Scrollbar(
            frame,
            command=self.listbox.yview,
            width=16,
            relief="flat",
            borderwidth=0,
        )
        scrollbar.pack(side="right", fill="y")
        self.listbox.configure(yscrollcommand=scrollbar.set)
        self.listbox.bind("<Configure>", self._list_resized, add="+")

        def release_hot_zone(event: Any) -> str | None:
            # ButtonRelease reaches the Toplevel reliably on the CH347 touch
            # bridge even when Tk missed the corresponding widget-local press.
            x_root = int(event.x_root)
            y_root = int(event.y_root)

            def inside(widget: Any | None) -> bool:
                return bool(widget) and (
                    widget.winfo_rootx() <= x_root < widget.winfo_rootx() + widget.winfo_width()
                    and widget.winfo_rooty() <= y_root < widget.winfo_rooty() + widget.winfo_height()
                )

            if inside(self.close_zone):
                self.hide_from_ui()
                return "break"
            if inside(self.clear_zone):
                self.on_clear()
                return "break"
            return None

        panel.bind("<ButtonRelease-1>", release_hot_zone, add="+")
        panel.bind("<Escape>", lambda _event: self.hide_from_ui())

    def _panel_rect(self) -> UiRect:
        return adaptive_panel_rect(
            self.root.winfo_screenwidth(),
            self.root.winfo_screenheight(),
            width_ratio=0.96,
            height_ratio=0.88,
            minimum_width=260,
            minimum_height=260,
            maximum_width=520,
            maximum_height=760,
            anchor="top-right",
        )

    def _geometry(self) -> str:
        return self._panel_rect().geometry()

    def show(self) -> None:
        self._create_panel()
        assert self.panel is not None
        rect = self._panel_rect()
        self.panel.geometry(rect.geometry())
        self._wrap_limit = notification_wrap_limit(max(1, rect.width - 48))
        notifications = self.service.store.list()
        self.refresh(
            notifications,
            maximum=INITIAL_RENDER_NOTIFICATIONS,
            defer_remaining=True,
        )
        self.panel.deiconify()
        self.panel.lift()
        self.panel.attributes("-topmost", True)

    def hide(self) -> None:
        if self.panel is not None:
            self.panel.withdraw()

    def hide_from_ui(self) -> None:
        self.service.set_visible(False, notify_ui=False)
        self.hide()

    def _list_resized(self, event: Any) -> None:
        wrap_limit = notification_wrap_limit(int(getattr(event, "width", 0)))
        if wrap_limit == self._wrap_limit:
            return
        self._wrap_limit = wrap_limit
        self.refresh(self._notifications)

    def refresh(
        self,
        notifications: list[dict[str, Any]],
        *,
        maximum: int | None = None,
        defer_remaining: bool = False,
    ) -> None:
        self._notifications = [dict(item) for item in notifications]
        self._render_revision += 1
        revision = self._render_revision
        self._render(maximum)
        if (
            defer_remaining
            and maximum is not None
            and len(self._notifications) > maximum
        ):
            self.root.after_idle(lambda: self._finish_deferred_render(revision))

    def _finish_deferred_render(self, revision: int) -> None:
        if revision == self._render_revision:
            self._render(None)

    def _render(self, maximum: int | None) -> None:
        if self.panel is None or self.listbox is None:
            return
        self.listbox.delete(0, "end")
        wrap_limit = self._wrap_limit or notification_wrap_limit(
            int(self.listbox.winfo_width())
        )
        self._wrap_limit = wrap_limit
        for line in notification_lines(
            self._notifications if maximum is None else self._notifications[:maximum],
            character_limit=wrap_limit,
        ):
            self.listbox.insert("end", line)
        if self.count_label is not None:
            self.count_label.configure(text=str(len(self._notifications)))


def run_tk() -> int:
    timing = startup_timing_enabled()
    startup_previous = _MODULE_ENTRY_STARTED
    startup_seen: set[str] = set()

    def mark_startup(phase: str) -> None:
        nonlocal startup_previous
        if not timing or phase in startup_seen:
            return
        now = time.perf_counter()
        print(
            "notification-center: startup "
            f"phase={phase} elapsed_ms={(now - _MODULE_ENTRY_STARTED) * 1000:.1f} "
            f"delta_ms={(now - startup_previous) * 1000:.1f}",
            flush=True,
        )
        startup_previous = now
        startup_seen.add(phase)

    mark_startup("module-entry")
    client = MsysClient.from_env()
    client.hello()
    for topic in sorted(NOTIFICATION_TOPICS):
        client.subscribe(topic)
    client.ready()
    mark_startup("mipc-ready")

    store = NotificationHistoryStore(history_path_from_env(), history_limit_from_env())
    actions: queue.Queue[tuple[str, Any]] = queue.Queue()
    service = NotificationCenterService(store, actions)
    mark_startup("history-loaded")

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
            print(f"notification-center: IPC failed: {exc}", flush=True)
            actions.put(("shutdown", None))

    # mIPC readiness means this on-demand role can already accept calls. Tk,
    # font enumeration and the first panel are intentionally below this edge:
    # a concurrent cold show is retained in the action queue and mapped as
    # soon as the target display is ready, without tripping Core's five-second
    # readiness timeout and restarting the now-warm process.
    threading.Thread(target=ipc_loop, name="msys-notification-center-ipc", daemon=True).start()

    import tkinter as tk
    mark_startup("tk-imported")

    root = tk.Tk(className=os.environ.get("MSYS_WINDOW_IDENTITY", "MsysNotificationCenter"))
    root.withdraw()
    mark_startup("root-created")
    configure_notification_fonts(root, default_size=10)
    configure_notification_window_identity(root)
    mark_startup("fonts-configured")
    root.title("msys-notification-center-host")
    client.event(
        "msys.role.ready",
        {"role": "notification-center", "component": client.component_id},
    )

    def clear_from_ui() -> None:
        service.handle_call({"type": "call", "id": 0, "method": "clear", "payload": {}})

    ui = NotificationCenterUi(root, service, clear_from_ui)

    def pump() -> None:
        while True:
            try:
                action, value = actions.get_nowait()
            except queue.Empty:
                break
            if action == "visibility":
                if value:
                    if ui.panel is None:
                        ui._create_panel()
                        mark_startup("first-panel-built")
                    ui.show()
                    mark_startup("first-show-dispatched")
                else:
                    ui.hide()
            elif action == "history":
                ui.refresh(value)
            elif action == "shutdown":
                ui.hide()
                root.destroy()
                return
        root.after(HOST_PUMP_INTERVAL_MS, pump)

    root.after(0, pump)
    root.mainloop()
    return 0


def main() -> int:
    try:
        return run_tk()
    except Exception as exc:
        print(f"notification-center: Tk failed: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
