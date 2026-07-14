from __future__ import annotations

import json
import os
import queue
import secrets
import tempfile
import textwrap
import threading
import time
from pathlib import Path
from typing import Any, Callable

from msys_sdk import MsysClient

from .adaptive import adaptive_panel_geometry
from .localization import shell_text
from msys_sdk.ui_fonts import configure_tk_fonts, font_spec


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

    def _create_panel(self) -> None:
        import tkinter as tk

        if self.panel is not None:
            return
        panel = tk.Toplevel(
            self.root,
            class_=os.environ.get("MSYS_WINDOW_IDENTITY", "MsysNotificationCenter"),
        )
        self.panel = panel
        panel.title(shell_text("notification.window_title"))
        panel.geometry(self._geometry())
        panel.configure(bg="#161c24")
        panel.attributes("-topmost", True)
        panel.resizable(True, True)
        panel.minsize(220, 180)
        panel.protocol("WM_DELETE_WINDOW", self.hide_from_ui)

        header = tk.Frame(panel, bg="#161c24")
        header.pack(fill="x", padx=10, pady=(9, 6))
        tk.Label(
            header,
            text=shell_text("notification.title"),
            bg="#161c24",
            fg="white",
            font=font_spec(panel, 13, "bold"),
            anchor="w",
        ).pack(side="left", expand=True, fill="x")
        self.count_label = tk.Label(
            header,
            text="0",
            bg="#161c24",
            fg="#8492a2",
            font=font_spec(panel, 9),
        )
        self.count_label.pack(side="left", padx=5)
        self.clear_zone = tk.Label(
            header,
            text=shell_text("notification.clear"),
            bg="#293441",
            fg="white",
            padx=8,
            pady=5,
            cursor="hand2",
        )
        self.clear_zone.pack(side="left", padx=3)
        self.close_zone = tk.Label(
            header,
            text=shell_text("notification.close"),
            bg="#293441",
            fg="white",
            padx=8,
            pady=5,
            cursor="hand2",
        )
        self.close_zone.pack(side="left", padx=3)

        frame = tk.Frame(panel, bg="#161c24")
        frame.pack(expand=True, fill="both", padx=10, pady=(0, 10))
        self.listbox = tk.Listbox(
            frame,
            bg="#222b35",
            fg="white",
            selectbackground="#34495e",
            selectforeground="white",
            activestyle="none",
            relief="flat",
            font=font_spec(panel, 10),
        )
        self.listbox.pack(side="left", expand=True, fill="both")
        scrollbar = tk.Scrollbar(frame, command=self.listbox.yview)
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
        panel.withdraw()

    def _geometry(self) -> str:
        return adaptive_panel_geometry(
            self.root,
            width_ratio=0.94,
            height_ratio=0.82,
            minimum_width=260,
            minimum_height=260,
            maximum_width=520,
            maximum_height=760,
            anchor="top-right",
        )

    def show(self) -> None:
        self._create_panel()
        assert self.panel is not None
        self.panel.geometry(self._geometry())
        self.panel.update_idletasks()
        self.refresh(self.service.store.list())
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

    def refresh(self, notifications: list[dict[str, Any]]) -> None:
        self._notifications = [dict(item) for item in notifications]
        if self.panel is None or self.listbox is None:
            return
        self.listbox.delete(0, "end")
        wrap_limit = self._wrap_limit or notification_wrap_limit(
            int(self.listbox.winfo_width())
        )
        self._wrap_limit = wrap_limit
        for line in notification_lines(
            self._notifications,
            character_limit=wrap_limit,
        ):
            self.listbox.insert("end", line)
        if self.count_label is not None:
            self.count_label.configure(text=str(len(self._notifications)))


def run_tk() -> int:
    import tkinter as tk

    root = tk.Tk(className=os.environ.get("MSYS_WINDOW_IDENTITY", "MsysNotificationCenter"))
    configure_tk_fonts(root, default_size=10)
    root.title("msys-notification-center-host")
    root.withdraw()
    root.update_idletasks()

    store = NotificationHistoryStore(history_path_from_env(), history_limit_from_env())
    actions: queue.Queue[tuple[str, Any]] = queue.Queue()
    service = NotificationCenterService(store, actions)

    def clear_from_ui() -> None:
        service.handle_call({"type": "call", "id": 0, "method": "clear", "payload": {}})

    ui = NotificationCenterUi(root, service, clear_from_ui)
    client = MsysClient.from_env()
    client.hello()
    for topic in sorted(NOTIFICATION_TOPICS):
        client.subscribe(topic)
    client.ready()
    client.event(
        "msys.role.ready",
        {"role": "notification-center", "component": client.component_id},
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
            print(f"notification-center: IPC failed: {exc}", flush=True)
            actions.put(("shutdown", None))

    def pump() -> None:
        while True:
            try:
                action, value = actions.get_nowait()
            except queue.Empty:
                break
            if action == "visibility":
                if value:
                    ui.show()
                else:
                    ui.hide()
            elif action == "history":
                ui.refresh(value)
            elif action == "shutdown":
                ui.hide()
                root.destroy()
                return
        root.after(40, pump)

    threading.Thread(target=ipc_loop, name="msys-notification-center-ipc", daemon=True).start()
    root.after(40, pump)
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
