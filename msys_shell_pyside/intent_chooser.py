from __future__ import annotations

import json
import os
import queue
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from msys_sdk import MsysClient
from msys_sdk.ui_layout import bind_tk_text_wrap

from .adaptive import adaptive_panel_geometry
from .localization import shell_text
from msys_sdk.ui_fonts import configure_tk_fonts, font_spec


PREFERENCE_SCHEMA = "msys.intent-preferences.v1"
DEFAULT_TIMEOUT_MS = 25000


def countdown_text(seconds: float) -> str:
    """Format the bounded UI countdown through the shared shell catalog."""

    return shell_text(
        "chooser.countdown.seconds",
        {"seconds": f"{max(0.0, float(seconds)):.1f}"},
    )


def preference_key(request: dict[str, Any]) -> str:
    """Return the stable scope used for an intent-handler preference.

    URI preferences are scoped to a scheme, MIME preferences to the concrete
    requested media type, and settings preferences to the panel name. Custom
    actions are scoped by action and any conventional discriminator supplied
    by the caller. Values such as a complete URI are deliberately excluded so
    opening a second resource can reuse the same handler safely.
    """

    action = str(request.get("action", "")).strip().lower()
    scope: dict[str, str] = {"action": action}
    if action == "open-uri":
        uri = str(request.get("uri", ""))
        scope["scheme"] = urllib.parse.urlsplit(uri).scheme.lower()
    elif action == "open-mime":
        scope["mime"] = str(request.get("mime", "")).strip().lower()
    elif action == "settings-panel":
        scope["name"] = str(request.get("name", "")).strip()
    else:
        for name in ("name", "mime", "scheme"):
            value = str(request.get(name, "")).strip()
            if value:
                scope[name] = value.lower() if name in {"mime", "scheme"} else value
    return json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class IntentCandidate:
    component: str
    name: str
    runtime: str
    priority: int = 0


def normalize_candidates(values: Any) -> list[IntentCandidate]:
    if not isinstance(values, list):
        return []
    result: list[IntentCandidate] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, dict):
            continue
        component = str(raw.get("component", "")).strip()
        if not component or component in seen:
            continue
        seen.add(component)
        try:
            priority = int(raw.get("priority", 0))
        except (TypeError, ValueError):
            priority = 0
        result.append(IntentCandidate(
            component=component,
            name=str(raw.get("name") or component),
            runtime=str(raw.get("runtime") or "application"),
            priority=priority,
        ))
    return result


class IntentPreferenceStore:
    """Small, provider-owned preference store with atomic replacement."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def _read_unlocked(self) -> dict[str, str]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict) or raw.get("schema") != PREFERENCE_SCHEMA:
            return {}
        values = raw.get("preferences", {})
        if not isinstance(values, dict):
            return {}
        return {
            str(key): str(value)
            for key, value in values.items()
            if isinstance(key, str) and isinstance(value, str) and key and value
        }

    def _write_unlocked(self, values: dict[str, str]) -> bool:
        directory = self.path.parent
        temporary: str | None = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=directory)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                    json.dump(
                        {"schema": PREFERENCE_SCHEMA, "preferences": values},
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
                # Some filesystems do not support syncing a directory. The
                # file itself has still been committed by atomic replacement.
                pass
            return True
        except OSError as exc:
            print(f"intent-chooser: cannot save preferences: {exc}", flush=True)
            return False
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def resolve(
        self,
        request: dict[str, Any],
        candidates: list[IntentCandidate],
    ) -> IntentCandidate | None:
        key = preference_key(request)
        allowed = {candidate.component: candidate for candidate in candidates}
        with self._lock:
            values = self._read_unlocked()
            selected = values.get(key)
            if selected in allowed:
                return allowed[selected]
            if selected is not None:
                # Installed handlers can disappear after an update. Do not let
                # a stale preference suppress the graphical chooser forever.
                values.pop(key, None)
                self._write_unlocked(values)
        return None

    def remember(self, request: dict[str, Any], component: str) -> bool:
        with self._lock:
            values = self._read_unlocked()
            values[preference_key(request)] = component
            return self._write_unlocked(values)

    def forget(self, request: dict[str, Any]) -> bool:
        with self._lock:
            values = self._read_unlocked()
            removed = values.pop(preference_key(request), None) is not None
            if removed:
                return self._write_unlocked(values)
            return removed

    def clear(self) -> int:
        with self._lock:
            values = self._read_unlocked()
            count = len(values)
            if count and not self._write_unlocked({}):
                return 0
            return count

    def list_preferences(self) -> dict[str, str]:
        with self._lock:
            return self._read_unlocked()


@dataclass(slots=True)
class PendingChoice:
    request_id: int
    request: dict[str, Any]
    candidates: list[IntentCandidate]
    timeout_ms: int
    event: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def complete(self, response: dict[str, Any]) -> bool:
        with self._lock:
            if self.response is not None:
                return False
            self.response = response
            self.event.set()
            return True


def preference_path_from_env() -> Path:
    explicit = os.environ.get("MSYS_CHOOSER_PREFERENCES")
    if explicit:
        return Path(explicit)
    state_dir = Path(os.environ.get("MSYS_STATE_DIR", "/opt/msys-state"))
    return state_dir / "preferences" / "intents.json"


def request_summary(request: dict[str, Any]) -> tuple[str, str]:
    action = str(request.get("action", "")).strip()
    if action == "open-uri":
        uri = str(request.get("uri", ""))
        scheme = urllib.parse.urlsplit(uri).scheme or shell_text("chooser.target.link")
        return shell_text("chooser.open_uri", scheme=scheme), uri
    if action == "open-mime":
        mime = str(request.get("mime") or shell_text("chooser.target.file"))
        return shell_text("chooser.open_mime", mime=mime), str(
            request.get("uri") or request.get("path") or ""
        )
    if action == "settings-panel":
        name = str(request.get("name") or shell_text("chooser.target.settings"))
        return shell_text("chooser.open_settings", name=name), ""
    return shell_text(
        "chooser.handle",
        action=action or shell_text("chooser.target.request"),
    ), str(
        request.get("uri") or ""
    )


class ChooserUi:
    def __init__(self, root: Any, store: IntentPreferenceStore) -> None:
        self.root = root
        self.store = store
        self.panel: Any | None = None
        self.pending: PendingChoice | None = None

    def dismiss(self, pending: PendingChoice | None = None) -> None:
        if pending is not None and self.pending is not pending:
            return
        panel = self.panel
        self.panel = None
        self.pending = None
        if panel is not None:
            try:
                panel.grab_release()
            except Exception:
                pass
            try:
                panel.destroy()
            except Exception:
                pass

    def cancel(self, code: str = "CHOICE_CANCELLED") -> None:
        pending = self.pending
        if pending is None:
            return
        pending.complete({
            "type": "error",
            "id": pending.request_id,
            "code": code,
            "message": "intent handler was not selected",
        })
        self.dismiss(pending)

    def show(self, pending: PendingChoice) -> None:
        import tkinter as tk

        if self.pending is not None:
            pending.complete({
                "type": "error",
                "id": pending.request_id,
                "code": "CHOOSER_BUSY",
                "message": "another choice is already visible",
            })
            return

        self.pending = pending
        print(
            f"intent-chooser: showing request id={pending.request_id} "
            f"candidates={len(pending.candidates)}",
            flush=True,
        )
        panel = tk.Toplevel(
            self.root,
            class_=os.environ.get("MSYS_WINDOW_IDENTITY", "MsysIntentChooser"),
        )
        self.panel = panel
        panel.title(shell_text("chooser.window_title"))
        panel.geometry(adaptive_panel_geometry(
            self.root,
            width_ratio=0.92,
            height_ratio=0.70,
            minimum_width=250,
            minimum_height=260,
            maximum_width=520,
            maximum_height=680,
        ))
        panel.configure(bg="#161c24")
        panel.attributes("-topmost", True)
        panel.resizable(True, True)
        panel.minsize(220, 220)
        panel.protocol("WM_DELETE_WINDOW", self.cancel)

        heading, detail = request_summary(pending.request)
        heading_label = tk.Label(
            panel,
            text=heading,
            bg="#161c24",
            fg="white",
            font=font_spec(panel, 13, "bold"),
            anchor="w",
            justify="left",
        )
        heading_label.pack(fill="x", padx=12, pady=(10, 2))
        bind_tk_text_wrap(heading_label, panel, horizontal_padding=32, minimum=120)
        if detail:
            detail_label = tk.Label(
                panel,
                text=detail,
                bg="#161c24",
                fg="#91a0b0",
                font=font_spec(panel, 9),
                anchor="w",
                justify="left",
            )
            detail_label.pack(fill="x", padx=12, pady=(0, 5))
            bind_tk_text_wrap(detail_label, panel, horizontal_padding=32, minimum=120)

        list_frame = tk.Frame(panel, bg="#161c24")
        list_frame.pack(expand=True, fill="both", padx=12, pady=4)
        choices = tk.Listbox(
            list_frame,
            bg="#252e39",
            fg="white",
            selectbackground="#3c79c6",
            selectforeground="white",
            activestyle="none",
            exportselection=False,
            relief="flat",
            font=font_spec(panel, 11),
            height=min(max(len(pending.candidates), 2), 6),
        )
        choices.pack(side="left", expand=True, fill="both")
        if len(pending.candidates) > 6:
            scrollbar = tk.Scrollbar(list_frame, command=choices.yview)
            scrollbar.pack(side="right", fill="y")
            choices.configure(yscrollcommand=scrollbar.set)
        for candidate in pending.candidates:
            choices.insert("end", f"{candidate.name}  [{candidate.runtime}]")
        choices.selection_set(0)

        selected_detail = tk.StringVar(value=pending.candidates[0].component)

        def update_detail(_event: Any = None) -> None:
            selection = choices.curselection()
            if selection:
                selected_detail.set(pending.candidates[int(selection[0])].component)

        choices.bind("<<ListboxSelect>>", update_detail)
        selected_label = tk.Label(
            panel,
            textvariable=selected_detail,
            bg="#161c24",
            fg="#7f8b99",
            font=font_spec(panel, 8),
            anchor="w",
            justify="left",
        )
        selected_label.pack(fill="x", padx=12)
        bind_tk_text_wrap(selected_label, panel, horizontal_padding=32, minimum=120)

        remember = tk.BooleanVar(value=False)
        remember_button = tk.Checkbutton(
            panel,
            text=shell_text("chooser.remember"),
            variable=remember,
            bg="#161c24",
            fg="white",
            selectcolor="#252e39",
            activebackground="#161c24",
            activeforeground="white",
            anchor="w",
            justify="left",
        )
        remember_button.pack(fill="x", padx=9, pady=2)
        bind_tk_text_wrap(remember_button, panel, horizontal_padding=32, minimum=120)

        footer = tk.Frame(panel, bg="#161c24")
        footer.pack(fill="x", padx=10, pady=(2, 8))
        countdown = tk.Label(
            footer,
            text="",
            bg="#161c24",
            fg="#8794a3",
            font=font_spec(panel, 8),
        )
        countdown.pack(side="left", padx=2)

        def accept(_event: Any = None) -> None:
            selection = choices.curselection()
            if not selection:
                return
            candidate = pending.candidates[int(selection[0])]
            remembered = False
            if remember.get():
                remembered = self.store.remember(pending.request, candidate.component)
            pending.complete({
                "type": "return",
                "id": pending.request_id,
                "payload": {
                    "component": candidate.component,
                    "remembered": remembered,
                    "preference_key": preference_key(pending.request),
                },
            })
            self.dismiss(pending)

        cancel_button = tk.Button(
            footer,
            text=shell_text("chooser.cancel"),
            command=self.cancel,
            width=7,
        )
        cancel_button.pack(side="right", padx=3)
        open_button = tk.Button(
            footer,
            text=shell_text("chooser.open"),
            command=accept,
            width=7,
        )
        open_button.pack(side="right", padx=3)

        def footer_release(event: Any) -> str | None:
            # Like the navigation bar, keep one Toplevel release hot-zone as a
            # touch fallback. Some minimal X11 input bridges deliver a valid
            # release without Tk observing the matching widget-local press,
            # in which case the standard Button class binding does not invoke
            # its command.
            x_root = int(event.x_root)
            y_root = int(event.y_root)

            def inside(widget: Any) -> bool:
                return (
                    widget.winfo_rootx() <= x_root < widget.winfo_rootx() + widget.winfo_width()
                    and widget.winfo_rooty() <= y_root < widget.winfo_rooty() + widget.winfo_height()
                )

            if inside(open_button):
                accept()
                return "break"
            if inside(cancel_button):
                self.cancel()
                return "break"
            return None

        panel.bind("<ButtonRelease-1>", footer_release, add="+")
        choices.bind("<Double-Button-1>", accept)
        panel.bind("<Return>", accept)
        panel.bind("<Escape>", lambda _event: self.cancel())

        started = time.monotonic()

        def update_countdown() -> None:
            if self.pending is not pending:
                return
            left = max(0.0, pending.timeout_ms / 1000 - (time.monotonic() - started))
            countdown.configure(text=countdown_text(left))
            if left > 0:
                self.root.after(200, update_countdown)

        update_countdown()
        panel.update_idletasks()
        panel.deiconify()
        panel.lift()
        # Do not take a global Tk/X grab. System navigation and the shield are
        # separate replaceable roles and must remain able to dismiss/cover the
        # chooser on a touch-only device.
        choices.focus_set()


def _timeout_from_env() -> int:
    try:
        value = int(os.environ.get("MSYS_CHOOSER_TIMEOUT_MS", str(DEFAULT_TIMEOUT_MS)))
    except ValueError:
        value = DEFAULT_TIMEOUT_MS
    return min(120000, max(500, value))


class IntentChooserService:
    """mIPC method implementation separated from the Tk event loop."""

    def __init__(
        self,
        store: IntentPreferenceStore,
        actions: queue.Queue[tuple[str, Any]],
        timeout_ms: int,
    ) -> None:
        self.store = store
        self.actions = actions
        self.timeout_ms = timeout_ms
        self._active_lock = threading.Lock()
        self._active: PendingChoice | None = None

    def handle_call(self, message: dict[str, Any]) -> dict[str, Any]:
        request_id = int(message.get("id", 0))
        method = str(message.get("method", ""))
        payload = message.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        request = payload.get("request", {})
        if not isinstance(request, dict):
            request = {}

        if method == "cancel_choice":
            with self._active_lock:
                pending = self._active
            if pending is None:
                return {
                    "type": "return",
                    "id": request_id,
                    "payload": {"cancelled": False, "visible": False},
                }
            cancelled = pending.complete({
                "type": "error",
                "id": pending.request_id,
                "code": "CHOICE_CANCELLED",
                "message": "intent handler selection was cancelled",
            })
            self.actions.put(("dismiss", pending))
            return {
                "type": "return",
                "id": request_id,
                "payload": {"cancelled": cancelled, "visible": False},
            }

        if method == "choose_intent":
            candidates = normalize_candidates(payload.get("candidates"))
            if not request.get("action") or not candidates:
                return {
                    "type": "error",
                    "id": request_id,
                    "code": "BAD_CHOICE_REQUEST",
                    "message": "request.action and at least one candidate are required",
                }
            remembered = self.store.resolve(request, candidates)
            if remembered is not None:
                return {
                    "type": "return",
                    "id": request_id,
                    "payload": {
                        "component": remembered.component,
                        "remembered": True,
                        "preference_key": preference_key(request),
                    },
                }
            if len(candidates) == 1:
                return {
                    "type": "return",
                    "id": request_id,
                    "payload": {"component": candidates[0].component, "remembered": False},
                }
            timeout_ms = self.timeout_ms
            deadline_ms = message.get("deadline_ms")
            if isinstance(deadline_ms, (int, float)) and not isinstance(deadline_ms, bool):
                # Reserve enough time for broker timeout handling and the
                # original public/component reply. This also ensures Back can
                # issue cancel_choice before the role call itself expires.
                remaining_ms = int(float(deadline_ms) - time.monotonic() * 1000) - 1500
                timeout_ms = min(timeout_ms, max(1, remaining_ms))
            print(
                "intent-chooser: choice requested "
                f"candidates={len(candidates)} timeout_ms={timeout_ms} "
                f"deadline_ms={deadline_ms}",
                flush=True,
            )
            pending = PendingChoice(request_id, dict(request), candidates, timeout_ms)
            with self._active_lock:
                if self._active is not None:
                    return {
                        "type": "error",
                        "id": request_id,
                        "code": "CHOOSER_BUSY",
                        "message": "another choice is already visible",
                    }
                self._active = pending
            self.actions.put(("show", pending))
            try:
                if not pending.event.wait((timeout_ms + 250) / 1000):
                    pending.complete({
                        "type": "error",
                        "id": request_id,
                        "code": "CHOICE_TIMEOUT",
                        "message": "intent handler was not selected in time",
                    })
                # Whether selected, cancelled, or timed out, ensure the Tk
                # surface is destroyed. The service thread owns the single
                # timeout so UI and broker cannot race to return different
                # outcomes near the deadline.
                self.actions.put(("dismiss", pending))
                assert pending.response is not None
                return pending.response
            finally:
                with self._active_lock:
                    if self._active is pending:
                        self._active = None
        if method == "forget_intent":
            return {
                "type": "return",
                "id": request_id,
                "payload": {
                    "removed": self.store.forget(request),
                    "preference_key": preference_key(request),
                },
            }
        if method == "clear_preferences":
            return {
                "type": "return",
                "id": request_id,
                "payload": {"removed": self.store.clear()},
            }
        if method == "list_preferences":
            return {
                "type": "return",
                "id": request_id,
                "payload": {"preferences": self.store.list_preferences()},
            }
        return {
            "type": "error",
            "id": request_id,
            "code": "NO_METHOD",
            "message": method,
        }


def run_tk() -> int:
    import tkinter as tk

    root = tk.Tk(className=os.environ.get("MSYS_WINDOW_IDENTITY", "MsysIntentChooser"))
    configure_tk_fonts(root, default_size=10)
    root.title("msys-intent-chooser-host")
    root.withdraw()
    root.update_idletasks()

    store = IntentPreferenceStore(preference_path_from_env())
    ui = ChooserUi(root, store)
    actions: queue.Queue[tuple[str, Any]] = queue.Queue()
    timeout_ms = _timeout_from_env()
    service = IntentChooserService(store, actions, timeout_ms)

    client = MsysClient.from_env()
    client.hello()
    client.ready()
    client.event("msys.role.ready", {"role": "chooser", "component": client.component_id})

    def ipc_loop() -> None:
        send_lock = threading.Lock()

        def handle_call(message: dict[str, Any]) -> None:
            response = service.handle_call(message)
            with send_lock:
                client.send(response)

        try:
            while True:
                message = client.recv(timeout=None)
                if not message or message.get("type") in {"eof", "shutdown"}:
                    actions.put(("shutdown", None))
                    return
                if message.get("type") != "call":
                    continue
                threading.Thread(
                    target=handle_call,
                    args=(message,),
                    name=f"msys-intent-chooser-call-{message.get('id', 0)}",
                    daemon=True,
                ).start()
        except Exception as exc:
            print(f"intent-chooser: IPC failed: {exc}", flush=True)
            actions.put(("shutdown", None))

    def pump() -> None:
        while True:
            try:
                action, value = actions.get_nowait()
            except queue.Empty:
                break
            if action == "show":
                try:
                    ui.show(value)
                except Exception as exc:
                    print(f"intent-chooser: cannot show choice: {exc}", flush=True)
                    value.complete({
                        "type": "error",
                        "id": value.request_id,
                        "code": "CHOOSER_UI_ERROR",
                        "message": str(exc),
                    })
                    ui.dismiss(value)
            elif action == "dismiss":
                ui.dismiss(value)
            elif action == "shutdown":
                ui.dismiss()
                root.destroy()
                return
        root.after(30, pump)

    threading.Thread(target=ipc_loop, name="msys-intent-chooser-ipc", daemon=True).start()
    root.after(30, pump)
    root.mainloop()
    return 0


def main() -> int:
    try:
        return run_tk()
    except Exception as exc:
        # This is an explicitly visual role. If X11 is unavailable, fail so
        # msysd can restart/quarantine it instead of advertising false ready.
        print(f"intent-chooser: Tk failed: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
