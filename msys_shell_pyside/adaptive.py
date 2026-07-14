from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class UiRect:
    width: int
    height: int
    x: int
    y: int

    def geometry(self) -> str:
        return f"{self.width}x{self.height}+{self.x}+{self.y}"


def _bounded(value: int, minimum: int, maximum: int) -> int:
    upper = max(1, int(maximum))
    lower = min(upper, max(1, int(minimum)))
    return min(upper, max(lower, int(value)))


def adaptive_panel_rect(
    screen_width: int,
    screen_height: int,
    *,
    width_ratio: float = 0.92,
    height_ratio: float = 0.76,
    minimum_width: int = 240,
    minimum_height: int = 220,
    maximum_width: int = 520,
    maximum_height: int = 720,
    margin: int = 8,
    anchor: str = "center",
) -> UiRect:
    """Fit an overlay panel to tiny mobile and larger desktop root windows."""

    screen_width = max(1, int(screen_width))
    screen_height = max(1, int(screen_height))
    margin = min(max(0, int(margin)), max(0, min(screen_width, screen_height) // 4))
    available_width = max(1, screen_width - margin * 2)
    available_height = max(1, screen_height - margin * 2)
    width = _bounded(
        round(screen_width * max(0.1, float(width_ratio))),
        minimum_width,
        min(maximum_width, available_width),
    )
    height = _bounded(
        round(screen_height * max(0.1, float(height_ratio))),
        minimum_height,
        min(maximum_height, available_height),
    )
    if anchor == "top-right":
        x = max(0, screen_width - width - margin)
        y = margin
    elif anchor == "top-left":
        x = margin
        y = margin
    else:
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2)
    return UiRect(width, height, x, y)


def adaptive_panel_geometry(widget: Any, **options: Any) -> str:
    return adaptive_panel_rect(
        widget.winfo_screenwidth(),
        widget.winfo_screenheight(),
        **options,
    ).geometry()


def full_screen_rect(screen_width: int, screen_height: int) -> UiRect:
    return UiRect(max(1, int(screen_width)), max(1, int(screen_height)), 0, 0)


def edge_bar_rect(screen_width: int, screen_height: int, edge: str) -> UiRect:
    """Provide useful first-map geometry before native X11 policy reflows it."""

    width = max(1, int(screen_width))
    height = max(1, int(screen_height))
    short = min(width, height)
    thickness = min(64, max(42, short // 10))
    if edge == "top":
        return UiRect(width, thickness, 0, 0)
    if edge == "right":
        return UiRect(thickness, height, max(0, width - thickness), 0)
    if edge == "left":
        return UiRect(thickness, height, 0, 0)
    return UiRect(width, thickness, 0, max(0, height - thickness))
