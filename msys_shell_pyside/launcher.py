from __future__ import annotations

import hashlib
import json
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from msys_sdk import MsysClient, Translator

from .localization import shell_text
from .preferences import (
    LauncherPreferenceService,
    PreferenceStore,
    default_preferences,
    preferences_path,
)
from msys_sdk.ui_fonts import configure_qt_fonts, configure_tk_fonts, font_spec
from msys_sdk.ui_layout import bind_tk_text_wrap


MOBILE_LAYOUT = "mobile"
DESKTOP_LAYOUT = "desktop"
KIOSK_LAYOUT = "kiosk"
SUPPORTED_IMAGE_MIME = frozenset({
    "image/png",
    "image/gif",
    "image/x-portable-pixmap",
    "image/x-portable-graymap",
    "image/x-portable-bitmap",
})


@dataclass(frozen=True, slots=True)
class AppIcon:
    path: str
    size: int = 0
    mime: str = ""


@dataclass(frozen=True, slots=True)
class LauncherApp:
    component: str
    name: str
    runtime: str
    summary: str
    icons: tuple[AppIcon, ...]
    placeholder_text: str
    placeholder_color: str


def launcher_layout(profile: str | None, width: int, height: int) -> str:
    """Select the launcher presentation without depending on a GUI toolkit.

    An explicit product profile is authoritative.  ``auto``, an empty value,
    and unknown future profiles fall back to live geometry, which lets the
    same launcher survive an X11 mode or orientation change.
    """

    requested = str(profile or "").strip().lower()
    if requested == "desktop":
        return DESKTOP_LAYOUT
    if requested == "kiosk":
        return KIOSK_LAYOUT
    if requested == "mobile":
        return MOBILE_LAYOUT
    width = max(1, int(width))
    height = max(1, int(height))
    if width >= 600 or (width >= 480 and width > height):
        return DESKTOP_LAYOUT
    return MOBILE_LAYOUT


def desktop_grid_columns(width: int, minimum_cell_width: int = 112) -> int:
    """Return a bounded number of icon columns for a live content width."""

    usable = max(1, int(width) - 20)
    cell = max(72, int(minimum_cell_width))
    return max(1, min(12, usable // cell))


def responsive_icon_size(
    layout: str,
    requested: int,
    width: int,
    height: int,
) -> int:
    """Fit icon artwork to the live window while respecting user intent."""

    width = max(1, int(width))
    height = max(1, int(height))
    requested = min(96, max(40, int(requested)))
    short = min(width, height)
    if layout == KIOSK_LAYOUT:
        enlarged = min(144, max(64, round(requested * 1.5)))
        return max(40, min(enlarged, max(40, short // 3)))
    if layout == MOBILE_LAYOUT:
        return min(requested, max(40, min(80, short // 5)))
    return min(requested, max(40, min(96, short // 4)))


def photo_scale_factors(source_size: int, target_size: int) -> tuple[int, int]:
    """Choose bounded integer Tk zoom/subsample factors near the target."""

    source = max(1, int(source_size))
    target = max(1, int(target_size))
    if source > target:
        return 1, max(1, (source + target - 1) // target)
    best_zoom, best_subsample = 1, 1
    best_scaled = source
    for subsample in range(1, 9):
        zoom = min(8, target * subsample // source)
        if zoom < 1:
            continue
        scaled = source * zoom // subsample
        if best_scaled < scaled <= target:
            best_zoom, best_subsample, best_scaled = zoom, subsample, scaled
    return best_zoom, best_subsample


def sort_apps(items: Iterable[LauncherApp], order: str) -> list[LauncherApp]:
    """Return a deterministic desktop order independent of registry order."""

    values = list(items)
    if order == "component":
        return sorted(values, key=lambda item: item.component.casefold())
    return sorted(values, key=lambda item: (item.name.casefold(), item.component.casefold()))


def blend_colour(first: str, second: str, ratio: float) -> str:
    """Blend two validated #RRGGBB colours for lightweight Tk theming."""

    amount = min(1.0, max(0.0, float(ratio)))
    left = tuple(int(first[index:index + 2], 16) for index in (1, 3, 5))
    right = tuple(int(second[index:index + 2], 16) for index in (1, 3, 5))
    mixed = tuple(round(a + (b - a) * amount) for a, b in zip(left, right))
    return "#" + "".join(f"{value:02x}" for value in mixed)


def readable_foreground(background: str) -> str:
    red, green, blue = (int(background[index:index + 2], 16) for index in (1, 3, 5))
    luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    return "#111820" if luminance >= 150 else "#ffffff"


def _bounded_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "\N{HORIZONTAL ELLIPSIS}"


def _component_address(value: Any) -> str:
    """Keep exact component addressing while rejecting unusable input."""

    text = str(value or "").strip()
    if not text or "\x00" in text or len(text) > 512:
        return ""
    return text


def stable_placeholder(component: str, name: str) -> tuple[str, str]:
    """Generate stable initials and a readable color from application id."""

    label = _bounded_text(name, 128) or component.rsplit(":", 1)[-1]
    words = [part for part in label.replace("_", " ").replace("-", " ").split() if part]
    if len(words) >= 2:
        initials = (words[0][0] + words[1][0]).upper()
    else:
        initials = (words[0][:2] if words else "AP").upper()
    digest = hashlib.sha256(component.encode("utf-8", errors="replace")).digest()
    # Keep placeholder colors dark enough for white text and far enough from
    # the shell background to remain recognizable at a glance.
    red = 55 + digest[0] % 96
    green = 65 + digest[1] % 96
    blue = 75 + digest[2] % 96
    return initials, f"#{red:02x}{green:02x}{blue:02x}"


def _metadata_blocks(item: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    yield item
    for key in ("presentation", "metadata", "package_metadata", "_manifest_presentation"):
        value = item.get(key)
        if isinstance(value, Mapping):
            yield value
    package = item.get("package")
    if isinstance(package, Mapping):
        yield package


def _package_catalog_path(root: Path, value: Any) -> Path | None:
    """Resolve a manifest-owned catalog without allowing package escape."""

    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return None
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        return None
    try:
        package_root = root.resolve(strict=True)
        candidate = package_root.joinpath(*relative.parts).resolve(strict=True)
        if not candidate.is_relative_to(package_root) or not candidate.is_file():
            return None
    except (OSError, RuntimeError):
        return None
    return candidate


def _localized_manifest_value(
    spec: Any,
    field: str,
    default: str,
    package_root: Path,
    translators: dict[Path, Translator | None],
) -> str:
    if not isinstance(spec, Mapping):
        return default
    key = spec.get(field + "_key")
    if not isinstance(key, str) or not key:
        return default
    catalog_path = _package_catalog_path(package_root, spec.get("catalog"))
    if catalog_path is None:
        return default
    if catalog_path not in translators:
        try:
            translators[catalog_path] = Translator.from_file(catalog_path)
        except (OSError, UnicodeError, ValueError):
            translators[catalog_path] = None
    translator = translators[catalog_path]
    if translator is None:
        return default
    return _bounded_text(translator.text(key, fallback=default), 256) or default


def normalize_icons(item: Mapping[str, Any]) -> tuple[AppIcon, ...]:
    """Read icon declarations from current and forward-compatible summaries."""

    roots: list[Path] = []
    for block in _metadata_blocks(item):
        root = block.get("package_root") or block.get("root")
        if isinstance(root, str) and root:
            roots.append(Path(root))
    default_root = roots[0] if roots else None
    result: list[AppIcon] = []
    seen: set[str] = set()
    for block in _metadata_blocks(item):
        raw_icons = block.get("icons", [])
        if isinstance(raw_icons, (str, Mapping)):
            raw_icons = [raw_icons]
        if not isinstance(raw_icons, list):
            continue
        block_root_value = block.get("package_root") or block.get("root")
        block_root = Path(block_root_value) if isinstance(block_root_value, str) and block_root_value else default_root
        for raw in raw_icons:
            if isinstance(raw, str):
                path_value, size, mime = raw, 0, ""
            elif isinstance(raw, Mapping):
                path_value = str(raw.get("path", ""))
                try:
                    size = max(0, int(raw.get("size", 0)))
                except (TypeError, ValueError, OverflowError):
                    size = 0
                mime = _bounded_text(raw.get("mime", ""), 96).lower()
            else:
                continue
            if not path_value or "\x00" in path_value:
                continue
            if path_value.startswith("@package/"):
                path_value = path_value[len("@package/") :]
            path = Path(path_value)
            if not path.is_absolute() and block_root is not None:
                path = block_root / path
            normalized = str(path)
            if normalized in seen:
                continue
            seen.add(normalized)
            result.append(AppIcon(normalized, size, mime))
    return tuple(result)


def choose_icon(icons: Iterable[AppIcon], desired_size: int) -> AppIcon | None:
    candidates = list(icons)
    if not candidates:
        return None
    desired = max(1, int(desired_size))

    def score(icon: AppIcon) -> tuple[int, int, int, str]:
        # Tk supports common raster formats without Pillow. Prefer those, then
        # the smallest image that does not require upscaling.
        supported = not icon.mime or icon.mime in SUPPORTED_IMAGE_MIME
        declared = icon.size if icon.size > 0 else desired
        return (
            0 if supported else 1,
            0 if declared >= desired else 1,
            abs(declared - desired),
            icon.path,
        )

    return min(candidates, key=score)


def normalize_app(item: Mapping[str, Any]) -> LauncherApp | None:
    component = _component_address(item.get("id") or item.get("component"))
    if not component:
        return None
    manifest = item.get("_manifest_presentation")
    manifest_block = manifest if isinstance(manifest, Mapping) else {}
    name = _bounded_text(
        manifest_block.get("component_name") or item.get("name") or manifest_block.get("name")
        or component.rsplit(":", 1)[-1],
        128,
    )
    runtime = _bounded_text(item.get("runtime"), 64) or "custom"
    summary = _bounded_text(
        manifest_block.get("summary") or item.get("summary") or "",
        256,
    )
    initials, color = stable_placeholder(component, name)
    return LauncherApp(
        component=component,
        name=name,
        runtime=runtime,
        summary=summary,
        icons=normalize_icons(item),
        placeholder_text=initials,
        placeholder_color=color,
    )


def load_manifest_presentations(paths: Iterable[Path]) -> dict[str, dict[str, Any]]:
    """Build presentation metadata keyed by fully-qualified component id.

    This is a compatibility bridge for older msysd builds whose ``list_apps``
    response predates package icon fields. It is read-only and only consumes
    the same installed manifests already trusted by the daemon.
    """

    catalog: dict[str, dict[str, Any]] = {}
    translators: dict[Path, Translator | None] = {}
    for path in dict.fromkeys(Path(value) for value in paths):
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict) or raw.get("schema") != "msys.manifest.v1":
            continue
        package = raw.get("package", {})
        components = raw.get("components", [])
        if not isinstance(package, dict) or not isinstance(components, list):
            continue
        package_id = str(package.get("id", ""))
        if not package_id:
            continue
        package_i18n = package.get("x-msys-i18n")
        package_name = _localized_manifest_value(
            package_i18n,
            "name",
            str(package.get("name", package_id)),
            path.parent,
            translators,
        )
        package_summary = _localized_manifest_value(
            package_i18n,
            "summary",
            str(package.get("summary", "")),
            path.parent,
            translators,
        )
        def resolve_icons(raw_icons: Any) -> list[dict[str, Any]]:
            resolved_icons: list[dict[str, Any]] = []
            if not isinstance(raw_icons, list):
                return resolved_icons
            for icon in raw_icons:
                if not isinstance(icon, dict) or not icon.get("path"):
                    continue
                resolved = dict(icon)
                icon_path = str(resolved["path"])
                if icon_path.startswith("@package/"):
                    icon_path = icon_path[len("@package/") :]
                candidate = Path(icon_path)
                if not candidate.is_absolute():
                    candidate = path.parent / candidate
                resolved["path"] = str(candidate)
                resolved_icons.append(resolved)
            return resolved_icons

        package_icons = resolve_icons(package.get("icons", []))
        for component in components:
            if not isinstance(component, dict) or not component.get("id"):
                continue
            key = f"{package_id}:{component['id']}"
            component_icons = resolve_icons(component.get("icons", []))
            component_i18n = component.get("x-msys-i18n")
            if not isinstance(component_i18n, Mapping):
                component_i18n = package_i18n
            component_name = _localized_manifest_value(
                component_i18n,
                "name",
                str(component.get("name", "")),
                path.parent,
                translators,
            )
            component_summary = _localized_manifest_value(
                component_i18n,
                "summary",
                str(component.get("summary", package_summary)),
                path.parent,
                translators,
            )
            catalog[key] = {
                "name": package_name,
                "component_name": component_name,
                "summary": component_summary,
                "vendor": package.get("vendor", ""),
                "icons": component_icons or package_icons,
                "package_root": str(path.parent),
            }
    return catalog


def manifest_paths_from_env(env: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    values = os.environ if env is None else env
    paths: list[Path] = []
    config_dir = values.get("MSYS_CONFIG_DIR", "")
    if config_dir:
        paths.extend(sorted((Path(config_dir) / "manifests").glob("**/*.json")))
    # Built-in providers receive the directory containing their manifest as
    # MSYS_PACKAGE_ROOT. The stock host hook intentionally need not export its
    # --config argv, so this also discovers peer built-in application metadata.
    package_root = values.get("MSYS_PACKAGE_ROOT", "")
    if package_root and Path(package_root).is_dir():
        paths.extend(sorted(Path(package_root).glob("**/*.json")))
    state_dir = Path(values.get("MSYS_STATE_DIR", "/opt/msys-state"))
    try:
        registry = json.loads((state_dir / "registry" / "installed.json").read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        registry = {}
    if isinstance(registry, dict):
        packages = registry.get("packages", [])
        if isinstance(packages, list):
            for package in packages:
                if isinstance(package, dict) and package.get("path"):
                    paths.append(Path(str(package["path"])) / "manifest.json")
    explicit = values.get("MSYS_MANIFEST_PATHS", "")
    for value in explicit.split(os.pathsep) if explicit else []:
        candidate = Path(value)
        if candidate.is_dir():
            paths.extend(sorted(candidate.glob("**/*.json")))
        elif candidate:
            paths.append(candidate)
    return tuple(dict.fromkeys(paths))


def list_launchable_components(
    *,
    env: Mapping[str, str] | None = None,
    public_call: Any | None = None,
) -> list[dict[str, Any]]:
    """Return core-authorized applications enriched with presentation data.

    ``msys.core`` remains authoritative for what is launchable.  The registry
    lookup only fills presentation fields which older daemon summaries do not
    expose yet; it can never turn a non-launchable manifest component into an
    application.  The optional arguments make the boundary deterministic for
    development tools and contract tests without introducing another service.
    """

    caller = public_call or MsysClient.public_call
    try:
        response = caller("msys.core", "list_apps", {}, timeout=4)
    except Exception as exc:
        print(f"launcher list components failed: {exc}", flush=True)
        return []
    if not isinstance(response, Mapping):
        return []
    payload = response.get("payload", {})
    if not isinstance(payload, Mapping):
        return []
    raw_items = payload.get("apps", [])
    if not isinstance(raw_items, list):
        return []
    catalog = load_manifest_presentations(manifest_paths_from_env(env))
    result: list[dict[str, Any]] = []
    for value in raw_items:
        if not isinstance(value, dict) or not value.get("id"):
            continue
        item = dict(value)
        presentation = catalog.get(str(item["id"]))
        if presentation is not None:
            item["_manifest_presentation"] = presentation
        result.append(item)
    return result


def start_component(
    component: str,
    *,
    public_call: Any | None = None,
    timeout: int = 12,
) -> dict[str, Any]:
    """Start one exact registry component through the public core API."""

    component_id = _component_address(component)
    if not component_id:
        raise ValueError("component must be non-empty")
    caller = public_call or MsysClient.public_call
    response = caller(
        "msys.core",
        "start",
        {"component": component_id},
        timeout=max(1, int(timeout)),
    )
    if not isinstance(response, dict):
        raise RuntimeError("msys.core start returned a non-object response")
    if response.get("type") != "return":
        code = str(response.get("code") or "START_FAILED")
        message = str(response.get("message") or "component start failed")
        raise RuntimeError(f"{code}: {message}")
    payload = response.get("payload", {})
    if not isinstance(payload, dict):
        raise RuntimeError("msys.core start returned a non-object payload")
    activation_error = payload.get("activation_error")
    if isinstance(activation_error, dict):
        code = str(activation_error.get("code") or "WINDOW_ACTIVATION_FAILED")
        message = str(activation_error.get("message") or "application window was not activated")
        raise RuntimeError(f"{code}: {message}")
    activation = payload.get("activation")
    if isinstance(activation, dict) and activation.get("ok") is False:
        detail = activation.get("reason") or activation.get("stderr") or "application window was not activated"
        raise RuntimeError(str(detail))
    return response


def _launcher_ipc_loop(
    client: MsysClient,
    service: LauncherPreferenceService,
    *,
    on_event: Any | None = None,
) -> None:
    """Consume the component channel once, including role calls and events."""

    try:
        while True:
            message = client.recv(timeout=None)
            if not message or message.get("type") in {"eof", "shutdown"}:
                return
            if message.get("type") == "event":
                if on_event is not None:
                    on_event(message)
            elif message.get("type") == "call":
                client.send(service.handle_call(message))
    except Exception as exc:
        print(f"launcher IPC failed: {exc}", flush=True)


def run_headless(client: MsysClient) -> int:
    profile = os.environ.get("MSYS_LAYOUT_PROFILE", "auto")
    service = LauncherPreferenceService(
        PreferenceStore(preferences_path(), profile=profile),
        publish=client.event,
    )
    client.hello()
    client.subscribe("msys.install.package_changed")
    client.subscribe("msys.layout.changed")
    client.ready()
    client.event("msys.shell.ready", {"component": client.component_id, "mode": "headless"})
    _launcher_ipc_loop(
        client,
        service,
        on_event=lambda msg: print(f"launcher event: {msg}", flush=True),
    )
    return 0


class LauncherTkUi:
    """Responsive Tk launcher; protocol and layout logic remain testable."""

    def __init__(
        self,
        root: Any,
        client: MsysClient,
        preferences: Mapping[str, Any] | None = None,
    ) -> None:
        import tkinter as tk

        self.tk = tk
        self.root = root
        self.client = client
        self.profile = os.environ.get("MSYS_LAYOUT_PROFILE", "auto")
        self.preferences = dict(preferences or default_preferences(self.profile))
        self.wallpaper_color = str(self.preferences["wallpaper_color"])
        self.accent_color = str(self.preferences["accent_color"])
        self.foreground_color = readable_foreground(self.wallpaper_color)
        self.panel_color = blend_colour(self.wallpaper_color, "#000000", 0.22)
        self.items: list[LauncherApp] = []
        self.refreshing = False
        self.render_after: str | None = None
        self.render_signature: tuple[Any, ...] | None = None
        self.images: list[Any] = []
        self.image_cache: dict[tuple[str, int], Any] = {}
        self.ui_actions: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self.internal_header_visible = True

        root.configure(bg=self.wallpaper_color)
        self.header = tk.Frame(root, bg=self.panel_color)
        self.header.pack(fill="x")
        self.title = tk.Label(
            self.header,
            text=shell_text("launcher.title.mobile"),
            bg=self.panel_color,
            fg=self.foreground_color,
            font=font_spec(self.root, 15, "bold"),
            anchor="w",
            padx=14,
            pady=8,
        )
        self.title.pack(side="left", expand=True, fill="x")
        self.refresh_button = tk.Label(
            self.header,
            text=shell_text("launcher.refresh"),
            bg=self.accent_color,
            fg=readable_foreground(self.accent_color),
            padx=10,
            pady=6,
            cursor="hand2",
        )
        self.refresh_button.pack(side="right", padx=9, pady=6)
        self.refresh_button.bind("<ButtonRelease-1>", lambda _event: self.refresh())

        self.status = tk.Label(
            root,
            text=shell_text("launcher.ready"),
            bg=self.wallpaper_color,
            fg=self.accent_color,
            anchor="w",
            padx=14,
            pady=3,
        )
        self.status.pack(fill="x")
        bind_tk_text_wrap(
            self.status,
            root,
            horizontal_padding=32,
            minimum=120,
        )

        self.viewport = tk.Frame(root, bg=self.wallpaper_color)
        self.viewport.pack(expand=True, fill="both")
        self.canvas = tk.Canvas(
            self.viewport,
            bg=self.wallpaper_color,
            highlightthickness=0,
            borderwidth=0,
        )
        self.scrollbar = tk.Scrollbar(self.viewport, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", expand=True, fill="both")
        self.scrollbar.pack(side="right", fill="y")
        self.content = tk.Frame(self.canvas, bg=self.wallpaper_color)
        self.content_window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", self._content_configured)
        self.canvas.bind("<Configure>", self._canvas_configured)
        root.bind("<Configure>", self._root_configured, add="+")
        self.canvas.bind_all("<MouseWheel>", self._mousewheel, add="+")
        self.canvas.bind_all("<Button-4>", lambda _event: self.canvas.yview_scroll(-1, "units"), add="+")
        self.canvas.bind_all("<Button-5>", lambda _event: self.canvas.yview_scroll(1, "units"), add="+")
        root.after(30, self._pump_ui_actions)

    def post_ui(self, action: Any) -> None:
        """Queue Tk work without calling Tcl from an mIPC/worker thread."""

        self.ui_actions.put(action)

    def _pump_ui_actions(self) -> None:
        while True:
            try:
                action = self.ui_actions.get_nowait()
            except queue.Empty:
                break
            action()
        try:
            self.root.after(30, self._pump_ui_actions)
        except self.tk.TclError:
            pass

    def _mousewheel(self, event: Any) -> None:
        delta = int(getattr(event, "delta", 0))
        if delta:
            self.canvas.yview_scroll(-1 if delta > 0 else 1, "units")

    def _content_configured(self, _event: Any) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _canvas_configured(self, event: Any) -> None:
        self.canvas.itemconfigure(self.content_window, width=max(1, int(event.width)))

    def _root_configured(self, event: Any) -> None:
        if event.widget is not self.root:
            return
        if self.render_after is not None:
            try:
                self.root.after_cancel(self.render_after)
            except self.tk.TclError:
                pass
        self.render_after = self.root.after(80, self.render_if_layout_changed)

    def _layout_signature(self) -> tuple[Any, ...]:
        width = max(self.root.winfo_width(), self.canvas.winfo_width(), 1)
        height = max(self.root.winfo_height(), 1)
        preferences = getattr(self, "preferences", {})
        requested = str(preferences.get("layout", self.profile))
        if requested == "profile":
            requested = self.profile
        requested_icon_size = int(preferences.get("icon_size", 56))
        mode = launcher_layout(requested, width, height)
        icon_size = responsive_icon_size(
            mode,
            requested_icon_size,
            width,
            height,
        )
        columns = (
            desktop_grid_columns(width, max(80, icon_size + 42))
            if mode == DESKTOP_LAYOUT
            else 1
        )
        return (
            mode,
            columns,
            width // 80,
            height // 80,
            icon_size,
            bool(preferences.get("show_labels", True)),
            str(preferences.get("sort", "name")),
            str(preferences.get("wallpaper_color", "#10151c")),
            str(preferences.get("accent_color", "#66b3ff")),
        )

    def render_if_layout_changed(self) -> None:
        self.render_after = None
        signature = self._layout_signature()
        if signature != self.render_signature:
            self.render()

    def set_status(self, text: str) -> None:
        value = _bounded_text(text, 256)
        self.post_ui(lambda: self.status.configure(text=value))

    def apply_preferences(self, preferences: Mapping[str, Any]) -> None:
        """Apply provider-validated preferences on the Tk thread."""

        self.preferences = dict(preferences)
        self.wallpaper_color = str(self.preferences["wallpaper_color"])
        self.accent_color = str(self.preferences["accent_color"])
        self.foreground_color = readable_foreground(self.wallpaper_color)
        self.panel_color = blend_colour(self.wallpaper_color, "#000000", 0.22)
        self.root.configure(bg=self.wallpaper_color)
        self.header.configure(bg=self.panel_color)
        self.title.configure(bg=self.panel_color, fg=self.foreground_color)
        self.refresh_button.configure(
            bg=self.accent_color,
            fg=readable_foreground(self.accent_color),
        )
        self.status.configure(bg=self.wallpaper_color, fg=self.accent_color)
        self.viewport.configure(bg=self.wallpaper_color)
        self.canvas.configure(bg=self.wallpaper_color)
        self.content.configure(bg=self.wallpaper_color)
        self.render_signature = None
        self.items = sort_apps(self.items, str(self.preferences.get("sort", "name")))
        self.render()

    def apply_layout_profile(self, profile: Any) -> None:
        value = str(profile or "").strip().lower()
        if value not in {"mobile", "desktop", "kiosk", "auto"}:
            return
        self.profile = value
        if self.preferences.get("layout") == "profile":
            self.render_signature = None
            self.render()

    def start(self, component: str) -> None:
        name = component.rsplit(":", 1)[-1]
        self.set_status(shell_text("launcher.starting", name=name))

        def worker() -> None:
            try:
                response = start_component(component)
                if response.get("type") == "return":
                    self.set_status(shell_text("launcher.started", name=name))
                else:
                    self.set_status(shell_text(
                        "launcher.start_failed",
                        message=str(response.get("code", "unknown error")),
                    ))
                print(f"launcher start response: {response}", flush=True)
            except Exception as exc:
                self.set_status(shell_text("launcher.start_failed", message=str(exc)))
                print(f"launcher start failed: {exc}", flush=True)

        threading.Thread(target=worker, name="msys-launcher-start", daemon=True).start()

    def refresh(self) -> None:
        if self.refreshing:
            return
        self.refreshing = True
        self.set_status(shell_text("launcher.refreshing"))
        sort_order = str(self.preferences.get("sort", "name"))

        def worker() -> None:
            error = ""
            try:
                raw = list_launchable_components()
                apps = [
                    app
                    for value in raw
                    if (app := normalize_app(value)) is not None
                ]
                apps = sort_apps(apps, sort_order)
            except Exception as exc:
                apps = []
                error = str(exc)
                print(f"launcher refresh failed: {exc}", flush=True)

            def complete() -> None:
                self.refreshing = False
                self.items = apps
                if error:
                    self.set_status(shell_text("launcher.refresh_failed", message=error))
                else:
                    self.set_status(shell_text(
                        "launcher.apps.one" if len(apps) == 1 else "launcher.apps.many",
                        count=len(apps),
                    ))
                self.render_signature = None
                self.render()

            self.post_ui(complete)

        threading.Thread(target=worker, name="msys-launcher-refresh", daemon=True).start()

    def _load_photo(self, app: LauncherApp, size: int) -> Any | None:
        icon = choose_icon(app.icons, size)
        if icon is None:
            return None
        key = icon.path, size
        if key in self.image_cache:
            return self.image_cache[key]
        try:
            photo = self.tk.PhotoImage(file=icon.path)
            source_size = max(photo.width(), photo.height(), 1)
            zoom, subsample = photo_scale_factors(source_size, size)
            if zoom > 1:
                photo = photo.zoom(zoom, zoom)
            if subsample > 1:
                photo = photo.subsample(subsample, subsample)
        except (self.tk.TclError, OSError):
            return None
        self.image_cache[key] = photo
        return photo

    def _icon_widget(self, parent: Any, app: LauncherApp, size: int) -> Any:
        photo = self._load_photo(app, size)
        if photo is not None:
            self.images.append(photo)
            return self.tk.Label(
                parent,
                image=photo,
                bg=str(parent.cget("bg")),
                width=size,
                height=size,
            )
        return self.tk.Label(
            parent,
            text=app.placeholder_text,
            bg=app.placeholder_color,
            fg="white",
            font=font_spec(self.root, max(11, size // 4), "bold"),
            width=max(2, size // 9),
            height=max(1, size // 20),
        )

    def _bind_launch(self, widget: Any, component: str) -> None:
        widget.configure(cursor="hand2")
        widget.bind("<ButtonRelease-1>", lambda _event, value=component: self.start(value))
        for child in widget.winfo_children():
            self._bind_launch(child, component)

    def _render_empty(self) -> None:
        self.tk.Label(
            self.content,
            text=shell_text("launcher.empty"),
            bg=self.wallpaper_color,
            fg=self.accent_color,
            font=font_spec(self.root, 11),
        ).pack(expand=True, pady=40)

    def _render_mobile(self, icon_size: int) -> None:
        row_color = blend_colour(self.wallpaper_color, "#000000", 0.18)
        row_foreground = readable_foreground(row_color)
        for app in self.items:
            row = self.tk.Frame(self.content, bg=row_color, padx=9, pady=7)
            row.pack(fill="x", padx=10, pady=4)
            icon = self._icon_widget(row, app, icon_size)
            icon.pack(side="left", padx=(0, 10))
            copy = self.tk.Frame(row, bg=row_color)
            copy.pack(side="left", expand=True, fill="both")
            name_label = self.tk.Label(
                copy,
                text=app.name,
                bg=row_color,
                fg=row_foreground,
                font=font_spec(self.root, 11, "bold"),
                anchor="w",
                justify="left",
                wraplength=max(120, self.canvas.winfo_width() - 100),
            )
            name_label.pack(fill="x")
            detail = app.summary or app.runtime
            self.tk.Label(
                copy,
                text=detail,
                bg=row_color,
                fg=self.accent_color,
                font=font_spec(self.root, 9),
                anchor="w",
                justify="left",
                wraplength=max(120, self.canvas.winfo_width() - 100),
            ).pack(fill="x", pady=(2, 0))
            self._bind_launch(row, app.component)

    def _render_desktop(self, columns: int, icon_size: int) -> None:
        show_labels = bool(self.preferences.get("show_labels", True))
        for column in range(columns):
            self.content.grid_columnconfigure(column, weight=1, uniform="apps")
        for index, app in enumerate(self.items):
            row, column = divmod(index, columns)
            # Desktop icons live directly on the wallpaper; their stable
            # component id, not the visible label, remains the launch target.
            tile = self.tk.Frame(self.content, bg=self.wallpaper_color, padx=6, pady=9)
            tile.grid(row=row, column=column, sticky="nsew", padx=6, pady=6)
            icon = self._icon_widget(tile, app, icon_size)
            icon.pack(pady=(1, 6 if show_labels else 1))
            if show_labels:
                self.tk.Label(
                    tile,
                    text=app.name,
                    bg=self.wallpaper_color,
                    fg=self.foreground_color,
                    font=font_spec(self.root, 10, "bold"),
                    justify="center",
                    wraplength=max(84, icon_size + 34),
                ).pack(expand=True, fill="x")
            self._bind_launch(tile, app.component)

    def _render_kiosk(self, icon_size: int) -> None:
        """Render sparse, large touch targets for single-purpose products."""

        show_labels = bool(self.preferences.get("show_labels", True))
        card_color = blend_colour(self.wallpaper_color, self.accent_color, 0.16)
        foreground = readable_foreground(card_color)
        for app in self.items:
            card = self.tk.Frame(self.content, bg=card_color, padx=12, pady=12)
            card.pack(expand=True, fill="both", padx=18, pady=12)
            icon = self._icon_widget(card, app, icon_size)
            icon.pack(expand=True, pady=(4, 10 if show_labels else 4))
            if show_labels:
                self.tk.Label(
                    card,
                    text=app.name,
                    bg=card_color,
                    fg=foreground,
                    font=font_spec(self.root, max(13, icon_size // 6), "bold"),
                    justify="center",
                    wraplength=max(140, self.canvas.winfo_width() - 72),
                ).pack(fill="x", pady=(0, 4))
            self._bind_launch(card, app.component)

    def _set_internal_header_visible(self, visible: bool) -> None:
        if self.internal_header_visible == bool(visible):
            return
        self.internal_header_visible = bool(visible)
        if visible:
            self.header.pack(fill="x", before=self.viewport)
            self.status.pack(fill="x", before=self.viewport)
        else:
            self.header.pack_forget()
            self.status.pack_forget()

    def render(self) -> None:
        self.images.clear()
        for child in self.content.winfo_children():
            child.destroy()
        # A desktop resize can reduce the grid column count. Reset weights for
        # now-empty old columns so they do not keep reserving blank space.
        for column in range(12):
            self.content.grid_columnconfigure(column, weight=0, uniform="")
        signature = self._layout_signature()
        self.render_signature = signature
        mode, columns, icon_size = signature[0], int(signature[1]), int(signature[4])
        self._set_internal_header_visible(mode == MOBILE_LAYOUT)
        self.title.configure(text=shell_text(
            "launcher.title.mobile" if mode == MOBILE_LAYOUT else "launcher.title.desktop"
        ))
        if not self.items:
            self._render_empty()
        elif mode == DESKTOP_LAYOUT:
            self._render_desktop(columns, icon_size)
        elif mode == KIOSK_LAYOUT:
            self._render_kiosk(icon_size)
        else:
            self._render_mobile(icon_size)
        self.content.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))


def _initial_geometry(root: Any) -> str:
    explicit = os.environ.get("MSYS_LAUNCHER_GEOMETRY")
    if explicit:
        return explicit
    return f"{max(1, root.winfo_screenwidth())}x{max(1, root.winfo_screenheight())}+0+0"


def run_tk(client: MsysClient) -> int:
    import tkinter as tk

    root = tk.Tk(className=os.environ.get("MSYS_WINDOW_IDENTITY", "MsysLauncher"))
    configure_tk_fonts(root, default_size=10)
    root.title(shell_text("launcher.window_title"))
    root.geometry(_initial_geometry(root))
    root.minsize(220, 160)
    profile = os.environ.get("MSYS_LAYOUT_PROFILE", "auto")
    service = LauncherPreferenceService(
        PreferenceStore(preferences_path(), profile=profile),
        publish=client.event,
    )
    ui = LauncherTkUi(root, client, service.preferences)
    service.on_change = lambda values: ui.post_ui(
        lambda snapshot=dict(values): ui.apply_preferences(snapshot)
    )

    # Do not publish readiness until Tk has connected to X and the launcher is
    # mapped. This lets msysd restart the visual role after DISPLAY is lost.
    root.update_idletasks()
    root.deiconify()
    root.update()
    print("launcher: hello", flush=True)
    client.hello()
    client.subscribe("msys.install.package_changed")
    client.subscribe("msys.layout.changed")
    print("launcher: ready", flush=True)
    client.ready()
    client.event("msys.shell.ready", {"component": client.component_id, "mode": "tk"})

    def on_event(message: dict[str, Any]) -> None:
        if message.get("topic") == "msys.install.package_changed":
            ui.post_ui(ui.refresh)
        elif message.get("topic") == "msys.layout.changed":
            payload = message.get("payload", {})
            if isinstance(payload, Mapping):
                ui.post_ui(
                    lambda value=payload.get("profile"): ui.apply_layout_profile(value)
                )

    threading.Thread(
        target=lambda: _launcher_ipc_loop(client, service, on_event=on_event),
        name="msys-launcher-ipc",
        daemon=True,
    ).start()
    ui.refresh()
    root.after(60000, lambda: _periodic_refresh(root, ui))
    root.mainloop()
    return 0


def _periodic_refresh(root: Any, ui: LauncherTkUi) -> None:
    ui.refresh()
    try:
        root.after(60000, lambda: _periodic_refresh(root, ui))
    except ui.tk.TclError:
        pass


def run_pyside(client: MsysClient) -> int:
    """Optional PySide entry point using the real manifest-backed app model."""

    from PySide6 import QtCore, QtGui, QtWidgets

    profile = os.environ.get("MSYS_LAYOUT_PROFILE", "auto")
    service = LauncherPreferenceService(
        PreferenceStore(preferences_path(), profile=profile),
        publish=client.event,
    )
    app = QtWidgets.QApplication(sys.argv)
    configure_qt_fonts(app, QtGui)
    window = QtWidgets.QWidget()
    window.setWindowTitle(shell_text("launcher.window_title"))
    outer = QtWidgets.QVBoxLayout(window)
    title = QtWidgets.QLabel(shell_text("launcher.title.mobile"))
    title.setAlignment(QtCore.Qt.AlignCenter)
    apps = QtWidgets.QListWidget()
    launch = QtWidgets.QPushButton(shell_text("launcher.launch"))
    outer.addWidget(title)
    outer.addWidget(apps)
    outer.addWidget(launch)
    raw_items = list_launchable_components()
    normalized = [entry for value in raw_items if (entry := normalize_app(value)) is not None]
    normalized = sort_apps(normalized, str(service.preferences.get("sort", "name")))
    for entry in normalized:
        item = QtWidgets.QListWidgetItem(entry.name)
        item.setData(QtCore.Qt.UserRole, entry.component)
        apps.addItem(item)
    window.resize(520, 600)
    window.show()
    app.processEvents()
    client.hello()
    client.subscribe("msys.install.package_changed")
    client.subscribe("msys.layout.changed")
    client.ready()
    client.event("msys.shell.ready", {"component": client.component_id, "mode": "pyside"})

    class PreferenceBridge(QtCore.QObject):
        changed = QtCore.Signal(object)

    bridge = PreferenceBridge()

    def apply_preferences(values: Mapping[str, Any]) -> None:
        background = str(values["wallpaper_color"])
        foreground = readable_foreground(background)
        accent = str(values["accent_color"])
        accent_foreground = readable_foreground(accent)
        window.setStyleSheet(
            "QWidget { background: " + background + "; color: " + foreground + "; } "
            "QPushButton { background: " + accent + "; color: " + accent_foreground + "; "
            "padding: 8px; border-radius: 5px; }"
        )

    bridge.changed.connect(apply_preferences)
    service.on_change = lambda values: bridge.changed.emit(dict(values))
    apply_preferences(service.preferences)

    def launch_selected() -> None:
        selected = apps.currentItem()
        if selected is None:
            return
        component = str(selected.data(QtCore.Qt.UserRole) or "")
        if component:
            def worker() -> None:
                try:
                    start_component(component)
                except Exception as exc:
                    print(f"launcher start failed: {exc}", flush=True)

            threading.Thread(
                target=worker,
                daemon=True,
            ).start()

    launch.clicked.connect(launch_selected)
    threading.Thread(
        target=lambda: _launcher_ipc_loop(
            client,
            service,
            on_event=lambda msg: print(f"launcher event: {msg}", flush=True),
        ),
        name="msys-launcher-ipc",
        daemon=True,
    ).start()
    return app.exec()


def main() -> int:
    client = MsysClient.from_env()
    if os.environ.get("MSYS_SHELL_HEADLESS") == "1":
        return run_headless(client)
    if os.environ.get("MSYS_LAUNCHER_UI", "tk") == "tk":
        return run_tk(client)
    try:
        return run_pyside(client)
    except Exception as exc:
        print(f"launcher UI failed: {exc}", flush=True)
        if os.environ.get("MSYS_LAUNCHER_HEADLESS_FALLBACK") == "1":
            time.sleep(0.1)
            return run_headless(client)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
