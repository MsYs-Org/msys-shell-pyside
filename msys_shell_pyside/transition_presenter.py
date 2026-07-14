from __future__ import annotations

import os
import queue
import threading
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from msys_sdk import MsysClient

from .localization import shell_text
from msys_sdk.ui_fonts import configure_tk_fonts, font_spec


TRANSITION_TOPIC = "msys.lifecycle.transition"
START_PHASES = frozenset({"launching", "closing"})
TERMINAL_PHASES = frozenset({"launched", "closed", "failed"})
PHASE_ALIASES = {
    "launch": "launching",
    "open": "launching",
    "opening": "launching",
    "close": "closing",
    "exit": "closing",
    "exiting": "closing",
}
DEFAULT_DURATION_MS = {
    "launching": 1800,
    "closing": 1100,
    "launched": 420,
    "closed": 420,
    "failed": 650,
}
MIN_DURATION_MS = 240
MAX_DURATION_MS = 4000
HARD_WITHDRAW_GRACE_MS = 250
MAX_TEXT_CHARS = 256


@dataclass(frozen=True, slots=True)
class TransitionView:
    revision: int
    phase: str
    component: str
    title: str
    identity: str
    duration_ms: int
    generation: int | None = None


@dataclass(frozen=True, slots=True)
class HideCommand:
    revision: int
    phase: str
    delay_ms: int = 0


def _text(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    result = str(value or "").strip()
    if len(result) <= limit:
        return result
    return result[: max(0, limit - 1)] + "\N{HORIZONTAL ELLIPSIS}"


def normalize_phase(value: Any) -> str:
    phase = str(value or "").strip().lower()
    phase = PHASE_ALIASES.get(phase, phase)
    if phase not in START_PHASES | TERMINAL_PHASES:
        raise ValueError(f"unsupported transition phase: {phase or '<empty>'}")
    return phase


def normalize_duration(value: Any, phase: str) -> int:
    if value is None or value == "":
        return DEFAULT_DURATION_MS[phase]
    if isinstance(value, bool):
        raise ValueError("duration_ms must be an integer")
    try:
        duration = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("duration_ms must be an integer") from exc
    return min(MAX_DURATION_MS, max(MIN_DURATION_MS, duration))


def normalize_generation(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        generation = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return generation if generation >= 0 else None


def make_transition(payload: Mapping[str, Any], revision: int) -> TransitionView:
    phase = normalize_phase(payload.get("phase"))
    component = _text(payload.get("component"))
    title = _text(payload.get("title"))
    if not title:
        title = (
            component.rsplit(":", 1)[-1]
            if component
            else shell_text("transition.application")
        )
    return TransitionView(
        revision=int(revision),
        phase=phase,
        component=component,
        title=title,
        identity=_text(payload.get("identity")),
        duration_ms=normalize_duration(payload.get("duration_ms"), phase),
        generation=normalize_generation(payload.get("generation")),
    )


def terminal_matches(active: TransitionView, payload: Mapping[str, Any]) -> bool:
    """Prevent a late completion for one app from hiding another app's mask."""

    component = _text(payload.get("component"))
    if active.component and component != active.component:
        return False
    incoming_generation = normalize_generation(payload.get("generation"))
    if (
        active.generation is not None
        and incoming_generation is not None
        and incoming_generation != active.generation
    ):
        return False
    return True


def fade_alpha(
    elapsed_ms: int,
    duration_ms: int,
    start: float,
    end: float,
) -> float:
    """Linear fade curve kept independent of Tk for deterministic tests."""

    duration = max(1, int(duration_ms))
    progress = min(1.0, max(0.0, int(elapsed_ms) / duration))
    return float(start) + (float(end) - float(start)) * progress


def eased_progress(frame: int, frames: int) -> float:
    """Cubic ease-out used by the compositor-free transition card."""

    total = max(1, int(frames))
    value = min(1.0, max(0.0, int(frame) / total))
    return 1.0 - (1.0 - value) ** 3


def transition_card_width(phase: str, progress: float) -> float:
    """Phase-specific card motion while the full-screen mask is visible."""

    value = min(1.0, max(0.0, float(progress)))
    if phase == "closing":
        return 0.72 - 0.16 * value
    return 0.48 + 0.24 * value


class TransitionPresenterService:
    """Thread-safe role behavior independent of its Tk presentation."""

    def __init__(self, actions: queue.Queue[tuple[str, Any]]) -> None:
        self.actions = actions
        self._lock = threading.RLock()
        self._revision = 0
        self._active: TransitionView | None = None
        self._last_phase = ""

    @property
    def active(self) -> TransitionView | None:
        with self._lock:
            return self._active

    def status(self) -> dict[str, Any]:
        with self._lock:
            active = self._active
            result: dict[str, Any] = {
                "visible": active is not None,
                "revision": self._revision,
                "last_phase": self._last_phase,
            }
            if active is not None:
                result["transition"] = asdict(active)
            return result

    def show(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._revision += 1
            view = make_transition(payload, self._revision)
            self._active = view
            self._last_phase = view.phase
        self.actions.put(("show", view))
        return self.status()

    def hide(self, *, phase: str = "", delay_ms: int = 0) -> dict[str, Any]:
        with self._lock:
            self._revision += 1
            revision = self._revision
            if phase:
                self._last_phase = phase
            self._active = None
        self.actions.put((
            "hide",
            HideCommand(
                revision=revision,
                phase=phase,
                delay_ms=max(0, min(1000, int(delay_ms))),
            ),
        ))
        return self.status()

    def expire(self, revision: int) -> bool:
        """Auto-hide only if the timer still owns the current presentation."""

        with self._lock:
            active = self._active
            if active is None or active.revision != int(revision):
                return False
        self.hide(phase="timeout")
        return True

    def handle_event(self, message: Mapping[str, Any]) -> bool:
        if message.get("topic") != TRANSITION_TOPIC:
            return False
        payload = message.get("payload", {})
        if not isinstance(payload, Mapping):
            return False
        try:
            phase = normalize_phase(payload.get("phase"))
        except ValueError as exc:
            print(f"transition-presenter: ignored event: {exc}", flush=True)
            return False
        if phase in START_PHASES:
            self.show(payload)
            return True
        active = self.active
        if active is None or not terminal_matches(active, payload):
            return False
        # A successful terminal event should reveal the real window quickly.
        # Failure remains readable for a moment before the same fade-out path.
        delay = 320 if phase == "failed" else 60
        self.hide(phase=phase, delay_ms=delay)
        return True

    def handle_call(self, message: Mapping[str, Any]) -> dict[str, Any]:
        request_id = int(message.get("id", 0))
        method = str(message.get("method", ""))
        payload = message.get("payload", {})
        if not isinstance(payload, Mapping):
            return self._error(request_id, "BAD_REQUEST", "payload must be an object")
        try:
            if method == "show":
                result = self.show(payload)
            elif method == "hide":
                result = self.hide(phase="manual")
            elif method == "status":
                result = self.status()
            else:
                return self._error(request_id, "NO_METHOD", method)
        except (TypeError, ValueError, OverflowError) as exc:
            return self._error(request_id, "BAD_REQUEST", str(exc))
        return {"type": "return", "id": request_id, "payload": result}

    @staticmethod
    def _error(request_id: int, code: str, message: str) -> dict[str, Any]:
        return {"type": "error", "id": request_id, "code": code, "message": message}


class TransitionTkUi:
    FADE_IN_MS = 150
    FADE_OUT_MS = 170
    TARGET_ALPHA = 0.92

    def __init__(self, root: Any, service: TransitionPresenterService) -> None:
        self.root = root
        self.service = service
        self.panel: Any | None = None
        self.card: Any | None = None
        self.heading: Any | None = None
        self.subtitle: Any | None = None
        self.progress: Any | None = None
        self._after_ids: set[str] = set()
        self._render_revision = 0
        self._alpha = 0.0
        self._alpha_supported = True

    def _later(self, delay_ms: int, callback: Any) -> str:
        identifier = ""

        def run() -> None:
            self._after_ids.discard(identifier)
            callback()

        identifier = self.root.after(max(0, int(delay_ms)), run)
        self._after_ids.add(identifier)
        return identifier

    def _cancel_scheduled(self) -> None:
        for identifier in list(self._after_ids):
            try:
                self.root.after_cancel(identifier)
            except Exception:
                pass
        self._after_ids.clear()

    def _ensure_panel(self) -> None:
        if self.panel is not None:
            return
        import tkinter as tk

        panel = tk.Toplevel(
            self.root,
            class_=os.environ.get("MSYS_WINDOW_IDENTITY", "MsysTransitionPresenter"),
        )
        self.panel = panel
        panel.title(shell_text("transition.window_title"))
        panel.configure(bg="#080c12")
        panel.attributes("-topmost", True)
        try:
            panel.attributes("-type", "splash")
        except tk.TclError:
            pass
        panel.protocol("WM_DELETE_WINDOW", lambda: self.service.hide(phase="window-close"))
        panel.bind("<Configure>", lambda _event: self._adapt())

        def dismiss(_event: Any) -> str:
            # The mask intentionally has no grab. A touch on it also provides
            # an immediate escape hatch if a lifecycle completion was lost.
            self.service.hide(phase="pointer-dismiss")
            return "break"

        panel.bind("<ButtonRelease-1>", dismiss, add="+")

        card = tk.Frame(panel, bg="#151d28", padx=18, pady=16)
        self.card = card
        card.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.72)
        self.heading = tk.Label(
            card,
            text="",
            bg="#151d28",
            fg="white",
            font=font_spec(panel, 18, "bold"),
            justify="center",
        )
        self.heading.pack(fill="x")
        self.subtitle = tk.Label(
            card,
            text="",
            bg="#151d28",
            fg="#91a4b8",
            font=font_spec(panel, 10),
            justify="center",
            pady=6,
        )
        self.subtitle.pack(fill="x")
        self.progress = tk.Canvas(
            card,
            height=4,
            bg="#252f3d",
            highlightthickness=0,
            borderwidth=0,
        )
        self.progress.pack(fill="x", pady=(7, 0))
        for surface in (card, self.heading, self.subtitle, self.progress):
            surface.bind("<ButtonRelease-1>", dismiss, add="+")
        panel.withdraw()

    def _screen_geometry(self) -> str:
        width = max(1, int(self.root.winfo_screenwidth()))
        height = max(1, int(self.root.winfo_screenheight()))
        return f"{width}x{height}+0+0"

    def _adapt(self) -> None:
        if self.panel is None or self.heading is None or self.subtitle is None:
            return
        width = max(1, int(self.panel.winfo_width()))
        height = max(1, int(self.panel.winfo_height()))
        short = min(width, height)
        heading_size = max(14, min(28, short // 20))
        self.heading.configure(
            font=font_spec(self.panel, heading_size, "bold"),
            wraplength=max(140, int(width * 0.62)),
        )
        self.subtitle.configure(wraplength=max(140, int(width * 0.62)))

    def _set_alpha(self, alpha: float) -> None:
        self._alpha = min(1.0, max(0.0, float(alpha)))
        if self.panel is None or not self._alpha_supported:
            return
        try:
            self.panel.attributes("-alpha", self._alpha)
        except Exception:
            # X11 without a compositing manager still gets the opaque mask and
            # bounded lifetime; the role never introduces a compositor.
            self._alpha_supported = False

    def _force_withdraw(self, revision: int) -> bool:
        """Direct Tk-thread watchdog; it does not depend on IPC or UI queues."""

        if self.panel is None or int(revision) != self._render_revision:
            return False
        try:
            self.panel.withdraw()
            self.panel.attributes("-topmost", False)
        except Exception:
            return False
        self._set_alpha(0.0)
        return True

    def _fade(
        self,
        revision: int,
        start: float,
        end: float,
        duration_ms: int,
        on_done: Any | None = None,
    ) -> None:
        steps = max(1, int(duration_ms) // 25)

        def frame(index: int) -> None:
            if revision != self._render_revision:
                return
            elapsed = min(int(duration_ms), index * int(duration_ms) // steps)
            self._set_alpha(fade_alpha(elapsed, duration_ms, start, end))
            if index >= steps:
                if on_done is not None:
                    on_done()
                return
            self._later(max(1, int(duration_ms) // steps), lambda: frame(index + 1))

        frame(0)

    @staticmethod
    def _labels(view: TransitionView) -> tuple[str, str]:
        if view.phase == "closing":
            return (
                shell_text("transition.closing", title=view.title),
                shell_text("transition.returning"),
            )
        if view.phase == "failed":
            return (
                shell_text("transition.open_failed", title=view.title),
                shell_text("transition.reported_failure"),
            )
        return (
            shell_text("transition.opening", title=view.title),
            shell_text("transition.preparing"),
        )

    def _animate_progress(self, revision: int, position: int = 0) -> None:
        if revision != self._render_revision or self.progress is None or self.panel is None:
            return
        width = max(1, int(self.progress.winfo_width()))
        segment = max(12, width // 4)
        start = (position % (width + segment)) - segment
        self.progress.delete("pulse")
        self.progress.create_rectangle(
            start,
            0,
            min(width, start + segment),
            4,
            fill="#66b3ff",
            outline="",
            tags="pulse",
        )
        self._later(45, lambda: self._animate_progress(revision, position + max(3, width // 28)))

    def _animate_card(self, revision: int, phase: str, frame: int = 0) -> None:
        if revision != self._render_revision or self.card is None:
            return
        frames = 8
        progress = eased_progress(frame, frames)
        self.card.place_configure(relwidth=transition_card_width(phase, progress))
        if frame < frames:
            self._later(22, lambda: self._animate_card(revision, phase, frame + 1))

    def show(self, view: TransitionView) -> None:
        self._ensure_panel()
        assert self.panel is not None and self.heading is not None and self.subtitle is not None
        self._cancel_scheduled()
        self._render_revision = view.revision
        heading, subtitle = self._labels(view)
        self.heading.configure(text=heading, fg="#ff9f82" if view.phase == "failed" else "white")
        self.subtitle.configure(text=subtitle)
        self.panel.geometry(self._screen_geometry())
        self._set_alpha(0.0)
        self.panel.update_idletasks()
        self.panel.deiconify()
        self.panel.lift()
        self.panel.attributes("-topmost", True)
        self._adapt()
        self._fade(view.revision, 0.0, self.TARGET_ALPHA, self.FADE_IN_MS)
        self._animate_card(view.revision, view.phase)
        self._animate_progress(view.revision)
        # Start the exit fade early enough that withdrawal is a true hard
        # deadline even if no terminal lifecycle event arrives.
        # Leave one IPC/UI-pump interval in addition to the fade itself.
        expiry_start = max(0, view.duration_ms - self.FADE_OUT_MS - 40)
        self._later(expiry_start, lambda: self.service.expire(view.revision))
        self._later(
            view.duration_ms + HARD_WITHDRAW_GRACE_MS,
            lambda: self._force_withdraw(view.revision),
        )

    def hide(self, command: HideCommand) -> None:
        if self.panel is None:
            return
        self._cancel_scheduled()
        self._render_revision = command.revision
        if command.phase == "failed" and self.heading is not None and self.subtitle is not None:
            self.heading.configure(text=shell_text("transition.failed"), fg="#ff9f82")
            self.subtitle.configure(text=shell_text("transition.returning"))

        def begin() -> None:
            start = self._alpha if self._alpha_supported else self.TARGET_ALPHA

            def withdraw() -> None:
                self._force_withdraw(command.revision)

            self._fade(command.revision, start, 0.0, self.FADE_OUT_MS, withdraw)

        self._later(command.delay_ms, begin)
        self._later(
            command.delay_ms + self.FADE_OUT_MS + HARD_WITHDRAW_GRACE_MS,
            lambda: self._force_withdraw(command.revision),
        )

    def shutdown(self) -> None:
        self._cancel_scheduled()
        if self.panel is not None:
            try:
                self.panel.withdraw()
                self.panel.destroy()
            except Exception:
                pass
            self.panel = None
            self.card = None
        self.root.destroy()


def run_tk() -> int:
    import tkinter as tk

    # The provider host itself stays withdrawn and one pixel large. Only the
    # short-lived mask Toplevel is mapped, so an idle provider cannot cover or
    # steal input from the desktop.
    root = tk.Tk(className=os.environ.get("MSYS_WINDOW_IDENTITY", "MsysTransitionPresenter"))
    configure_tk_fonts(root, default_size=10)
    root.title("msys-transition-presenter-host")
    root.geometry("1x1+0+0")
    root.withdraw()
    root.update_idletasks()

    actions: queue.Queue[tuple[str, Any]] = queue.Queue()
    service = TransitionPresenterService(actions)
    ui = TransitionTkUi(root, service)
    client = MsysClient.from_env()
    client.hello()
    client.subscribe(TRANSITION_TOPIC)
    client.ready()
    client.event(
        "msys.role.ready",
        {"role": "transition-presenter", "component": client.component_id},
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
            print(f"transition-presenter: IPC failed: {exc}", flush=True)
            actions.put(("shutdown", None))

    def pump() -> None:
        while True:
            try:
                action, value = actions.get_nowait()
            except queue.Empty:
                break
            if action == "show":
                ui.show(value)
            elif action == "hide":
                ui.hide(value)
            elif action == "shutdown":
                ui.shutdown()
                return
        root.after(30, pump)

    threading.Thread(target=ipc_loop, name="msys-transition-presenter-ipc", daemon=True).start()
    root.after(30, pump)
    root.mainloop()
    return 0


def main() -> int:
    try:
        return run_tk()
    except Exception as exc:
        print(f"transition-presenter: Tk failed: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
