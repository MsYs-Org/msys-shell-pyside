from __future__ import annotations

from dataclasses import dataclass


NAVIGATION_EDGES = frozenset({"bottom", "top", "left", "right"})
PILL_BACK_DISTANCE_PX = 16
PILL_RECENTS_DISTANCE_PX = 28
PILL_RECENTS_HOLD_MS = 420


def normalize_navigation_edge(value: object, fallback: str = "bottom") -> str:
    """Return a supported screen edge without trusting policy/env input."""

    edge = str(value or "").strip().lower()
    if edge in NAVIGATION_EDGES:
        return edge
    safe_fallback = str(fallback or "bottom").strip().lower()
    return safe_fallback if safe_fallback in NAVIGATION_EDGES else "bottom"


def infer_navigation_edge(
    width: int,
    height: int,
    *,
    root_x: int = 0,
    root_y: int = 0,
    screen_width: int | None = None,
    screen_height: int | None = None,
    preferred: object = None,
) -> str:
    """Infer bottom/top/left/right from a reflowed navigation surface.

    ``preferred`` is the explicit policy override.  Without one, a thin
    vertical surface is assigned to the closest horizontal screen edge and a
    thin horizontal surface to the closest vertical edge.  Missing screen
    dimensions retain the historic bottom/right defaults.
    """

    preferred_edge = str(preferred or "").strip().lower()
    if preferred_edge in NAVIGATION_EDGES:
        return preferred_edge
    safe_width = max(1, int(width))
    safe_height = max(1, int(height))
    if safe_height > safe_width:
        if screen_width is None or int(screen_width) <= 0:
            return "right"
        surface_center = int(root_x) + safe_width / 2
        return "left" if surface_center < int(screen_width) / 2 else "right"
    if screen_height is None or int(screen_height) <= 0:
        return "bottom"
    surface_center = int(root_y) + safe_height / 2
    return "top" if surface_center < int(screen_height) / 2 else "bottom"


def inward_distance(
    edge: str,
    start_x: int,
    start_y: int,
    current_x: int,
    current_y: int,
) -> int:
    """Return positive pointer travel from the navigation edge into content."""

    normalized = normalize_navigation_edge(edge)
    if normalized == "bottom":
        distance = int(start_y) - int(current_y)
    elif normalized == "top":
        distance = int(current_y) - int(start_y)
    elif normalized == "left":
        distance = int(current_x) - int(start_x)
    else:
        distance = int(start_x) - int(current_x)
    return max(0, distance)


@dataclass(frozen=True, slots=True)
class PillGestureUpdate:
    """One deterministic gesture transition consumed by the Tk presenter."""

    phase: str
    action: str | None = None
    inward_distance: int = 0
    progress: float = 0.0
    elapsed_ms: int = 0
    active: bool = False


class PillGestureStateMachine:
    """Distinguish tap, short Back swipe, and held Recents swipe.

    The machine does not own timers or UI.  A presenter calls :meth:`hold`
    from its event-loop timer and calls :meth:`move` for pointer motion.  The
    first transition satisfying both Recents thresholds emits ``apps`` and
    latches it, so release and duplicate motion can never dispatch again.
    """

    def __init__(
        self,
        *,
        back_distance: int = PILL_BACK_DISTANCE_PX,
        recents_distance: int = PILL_RECENTS_DISTANCE_PX,
        recents_hold_ms: int = PILL_RECENTS_HOLD_MS,
    ) -> None:
        self.back_distance = max(8, int(back_distance))
        self.recents_distance = max(self.back_distance + 1, int(recents_distance))
        self.recents_hold_ms = max(120, int(recents_hold_ms))
        self._active = False
        self._edge = "bottom"
        self._start_x = 0
        self._start_y = 0
        self._current_x = 0
        self._current_y = 0
        self._started_at = 0.0
        self._recents_triggered = False
        self._cancelled_until_release = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def edge(self) -> str:
        return self._edge

    def press(self, x: int, y: int, when: float, edge: str) -> PillGestureUpdate:
        self._active = True
        self._edge = normalize_navigation_edge(edge)
        self._start_x = self._current_x = int(x)
        self._start_y = self._current_y = int(y)
        self._started_at = float(when)
        self._recents_triggered = False
        self._cancelled_until_release = False
        return self._snapshot("tracking", when)

    def move(self, x: int, y: int, when: float) -> PillGestureUpdate:
        if not self._active:
            return PillGestureUpdate("idle")
        self._current_x = int(x)
        self._current_y = int(y)
        return self._evaluate(when)

    def hold(self, when: float) -> PillGestureUpdate:
        """Re-evaluate the stationary pointer when the UI hold timer fires."""

        if not self._active:
            return PillGestureUpdate("idle")
        return self._evaluate(when)

    def release(
        self,
        x: int,
        y: int,
        when: float,
        *,
        fallback_action: str,
    ) -> PillGestureUpdate:
        if not self._active:
            if self._cancelled_until_release:
                self._cancelled_until_release = False
                return PillGestureUpdate("released", active=False)
            return PillGestureUpdate(
                "released",
                action=str(fallback_action),
                active=False,
            )
        self._current_x = int(x)
        self._current_y = int(y)
        evaluated = self._evaluate(when)
        distance = evaluated.inward_distance
        elapsed_ms = evaluated.elapsed_ms
        progress = evaluated.progress
        if evaluated.action == "apps":
            action: str | None = "apps"
        elif self._recents_triggered:
            action = None
        elif distance >= self.back_distance:
            action = "close"
        else:
            action = str(fallback_action)
        self._reset()
        return PillGestureUpdate(
            "released",
            action=action,
            inward_distance=distance,
            progress=progress,
            elapsed_ms=elapsed_ms,
            active=False,
        )

    def cancel(self, when: float | None = None) -> PillGestureUpdate:
        if not self._active:
            return PillGestureUpdate("idle")
        snapshot = self._snapshot("cancelled", self._started_at if when is None else when)
        self._reset()
        self._cancelled_until_release = True
        return PillGestureUpdate(
            snapshot.phase,
            inward_distance=snapshot.inward_distance,
            progress=snapshot.progress,
            elapsed_ms=snapshot.elapsed_ms,
            active=False,
        )

    def _evaluate(self, when: float) -> PillGestureUpdate:
        snapshot = self._snapshot("tracking", when)
        if self._recents_triggered:
            return PillGestureUpdate(
                "triggered",
                inward_distance=snapshot.inward_distance,
                progress=snapshot.progress,
                elapsed_ms=snapshot.elapsed_ms,
                active=True,
            )
        if (
            snapshot.inward_distance >= self.recents_distance
            and snapshot.elapsed_ms >= self.recents_hold_ms
        ):
            self._recents_triggered = True
            return PillGestureUpdate(
                "triggered",
                action="apps",
                inward_distance=snapshot.inward_distance,
                progress=1.0,
                elapsed_ms=snapshot.elapsed_ms,
                active=True,
            )
        phase = "armed" if snapshot.inward_distance >= self.recents_distance else "tracking"
        return PillGestureUpdate(
            phase,
            inward_distance=snapshot.inward_distance,
            progress=snapshot.progress,
            elapsed_ms=snapshot.elapsed_ms,
            active=True,
        )

    def _snapshot(self, phase: str, when: float) -> PillGestureUpdate:
        distance = inward_distance(
            self._edge,
            self._start_x,
            self._start_y,
            self._current_x,
            self._current_y,
        )
        elapsed_ms = max(0, round((float(when) - self._started_at) * 1000))
        progress = min(1.0, distance / self.recents_distance)
        return PillGestureUpdate(
            phase,
            inward_distance=distance,
            progress=progress,
            elapsed_ms=elapsed_ms,
            active=self._active,
        )

    def _reset(self) -> None:
        self._active = False
        self._recents_triggered = False
