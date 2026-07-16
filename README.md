# msys-shell-pyside

Reference MSYS shell providers implemented in Python. They do not require
systemd, D-Bus, Openbox, a compositor, or a target package manager.

Current source version: `0.1.14`.

## Lean resident profile

Launcher, system chrome, and the selected gesture pill are the resident shell
surfaces in the lean pill profile. Transitions, toast presentation,
notification center, Recents, and the intent
chooser remain selected replaceable roles but start on their first role call
and are reclaimed after a bounded 15-60 second idle interval. The alternate
three-button provider retains the background lifecycle required by
`navigation-bar.v1`, but exclusive role selection keeps it dormant in a pill
profile; profiles that prefer it still get an always-visible navigation
surface.

The status agent is no longer required at boot. System chrome refreshes its
clock locally once per second and can still merge optional battery tick events
when an operator starts the on-demand status capability.

## Responsive launcher

The default launcher is implemented with the Python standard-library Tk
binding. It presents a touch-friendly list for `MSYS_LAYOUT_PROFILE=mobile`
and an application-icon desktop grid for `desktop`. `kiosk` is a distinct
sparse presentation with enlarged full-width application targets rather than
an alias for the mobile list. With
`MSYS_LAYOUT_PROFILE=auto` (or no recognized profile), live window dimensions
select the presentation. Both presentations reflow when X11 resizes the
window; the desktop grid changes its column count instead of assuming a fixed
320x480 display. Icon sizes are fitted again from the live geometry in all
three modes, while retaining the validated user preference as the upper-level
intent.

Applications still come exclusively from `msys.core.list_apps` and therefore
remain language/framework neutral. Component-level `icons` in
`msys.manifest.v1` override the package icon fallback when available. The launcher accepts icon metadata
directly in the core summary and also has a read-only compatibility lookup for
built-in manifests under `$MSYS_CONFIG_DIR/manifests` (or the provider's
`$MSYS_PACKAGE_ROOT`) and installed manifests
listed by `$MSYS_STATE_DIR/registry/installed.json`. Set
`MSYS_MANIFEST_PATHS` to an `os.pathsep`-separated set of extra development
manifest files/directories. Missing or Tk-unsupported images get deterministic
initials and a stable color derived from the component id.
This means an installed application such as `org.msys.settings:main` is still
shown as a usable desktop icon when its package does not ship raster artwork.
Clicking either the tile, its label, or its image starts that exact component
through `msys.core.start`; registry metadata is presentation-only and cannot
make an application launchable by itself.

Useful launcher settings:

```text
MSYS_LAUNCHER_UI=tk|pyside
MSYS_LAYOUT_PROFILE=mobile|desktop|kiosk|auto
MSYS_LAUNCHER_GEOMETRY=800x480+0+0   # optional development override
MSYS_UI_FONT_FAMILY=Noto Sans CJK SC  # optional installed-family override
MSYS_SHELL_HEADLESS=1                # protocol-only testing
```

The PySide path remains optional; the Tk path and all launcher model/layout
logic have no third-party Python dependency.

All shell Tk roots and the optional Qt path use the same installed-family
order from `msys_sdk.ui_fonts`. `MSYS_UI_FONT_FAMILY` is the cross-toolkit override;
`MSYS_TK_FONT_FAMILY` remains a compatibility alias. This policy does not
install or rasterize fonts: anti-aliased Tk text still requires an Xft-enabled
Tk runtime, while Qt uses its own Fontconfig/FreeType backend.

System releases expose the platform SDK directly. A standalone shell archive
can instead vendor that exact source at build time while keeping the source
tree deduplicated:

```sh
PYTHONPATH=../msys-tools python3 -m msys_tools.dev package build \
  . --root .. --output ../dist \
  --overlay msys-sdk/msys_sdk=msys_sdk
```

The active launcher provider owns desktop appearance through mIPC; Settings
does not edit its files. `role:launcher` implements:

```text
get_preferences({})
set_preferences({preferences: {layout?, wallpaper_color?, accent_color?, icon_size?, show_labels?, sort?}})
reset_preferences({})
```

The validated state is atomically stored under
`${MSYS_STATE_DIR}/shell/launcher.json` and changes are broadcast as
`msys.shell.preferences.changed`. Desktop icons are sorted deterministically,
reflow with the selected icon size, and sit directly on the wallpaper; mobile
mode retains touch-sized list rows. `layout=profile` is the default and follows
future product-profile changes; `auto`, `mobile`, `desktop`, or `kiosk` is an
explicit user override. Desktop mode removes the launcher's duplicate internal
header/status strip because the replaceable system-chrome role already owns
that screen edge.

## Lifecycle transition presenter

`python -m msys_shell_pyside.transition_presenter` implements the replaceable
exclusive `transition-presenter` role. It subscribes to
`msys.lifecycle.transition` and recognizes these core lifecycle phases:

```text
launching, launched, closing, closed, failed
```

`launching` and `closing` map a full-screen, topmost mask and fade it in while
the central card grows for launch or contracts for exit. A
matching terminal event fades it out; every presentation also has a bounded
hard Tk-thread withdrawal watchdog (at most four seconds plus fade grace), so
a lost event or stalled service queue cannot leave the screen blocked. A touch
on the mask dismisses it immediately. Component and
generation matching prevents a late completion for one process from hiding a
newer app's mask. The idle Tk host is withdrawn at 1x1, and the provider never
takes a local or global Tk grab. Alpha animation is used when X11 supports it;
on a non-composited display the same bounded opaque mask is used.

The mask publishes the stable `transition-presenter` identity and remains an
ordinary managed `overlay`; it does not attempt to implement stacking itself.
The X11 window policy places it above application/chrome/navigation surfaces
for the short lifecycle transaction and below the screen shield. Its matching
terminal-event fade, revision guard, direct Tk watchdog, and pointer escape
all end in `withdraw()`, so neither a stale completion nor a failed animation
callback can leave an idle input-owning surface mapped.

The role's mIPC methods are:

```text
show({phase, component, title, identity, duration_ms, generation?})
hide({})
status({})
```

`show.phase` also accepts the convenient `launch`/`close` aliases. A profile
can register the provider with the same ordinary manifest format as every
other component:

```json
{
  "id": "transitions",
  "runtime": "tk",
  "exec": ["python", "-m", "msys_shell_pyside.transition_presenter"],
  "lifecycle": "on-demand",
  "idle_timeout_ms": 15000,
  "restart": "on-failure",
  "windowing": {
    "system": "x11",
    "display": "inherit",
    "mode": "overlay",
    "title": "MSYS Transition",
    "identity": {
      "app_id": "org.msys.shell.transitions",
      "x11_wm_class": "org.msys.shell.transitions"
    }
  },
  "readiness": {"mode": "mipc-ready", "timeout_ms": 5000},
  "after": [
    "org.msys.openstick.ch347:x11-spi-touch-output",
    "org.msys.x11.session:hdmi-output"
  ],
  "provides": [{
    "role": "transition-presenter",
    "exclusive": true,
    "priority": 50
  }]
}
```

The repository's [`manifest.json`](manifest.json) is the canonical shell
manifest and contains this component declaration. Product profiles select the
provider but leave it out of boot startup; a typed role call starts it:

```json
{
  "roles": {
    "transition-presenter": ["org.msys.shell.pyside:transitions"]
  }
}
```

Mobile, mobile-pill, and desktop profiles use that role selection without a
resident transition process. A kiosk profile can omit the component and list
`transition-presenter` in `disabled_roles`.

## Other shell roles

`msys_shell_pyside.screen_shield` is the typed `screen-shield` role provider.
It starts as a withdrawn one-pixel host and maps the full-screen ownership
surface only after an explicit call:

```text
show({})   -> {visible, changed, revision, touch_dismiss_enabled, last_reason}
hide({})   -> {visible, changed, revision, touch_dismiss_enabled, last_reason}
toggle({}) -> {visible, changed, revision, touch_dismiss_enabled, last_reason}
status({}) -> {visible, revision, touch_dismiss_enabled, last_reason}
```

Repeated `show` or `hide` calls are idempotent. The compatibility broadcast
topic `msys.role.screen-shield` still accepts `action=show|hide|toggle`, but
role RPC is the primary control path. Touching the shield dismisses it by
default. Set `MSYS_SCREEN_SHIELD_TOUCH_DISMISS=0` in a product manifest or
provider environment to make touches stay captured without dismissing. The
provider never takes a Tk grab. If X11 destroys or unmaps the surface, its
logical state is reconciled to `visible=false`; a later `show` recreates it.

System chrome, navigation, shield, toast, chooser, notification-center, and
task-switcher first-map geometry is derived from the live X11 screen instead
of fixed device coordinates. Their Tk containers are resizable so the native
window-policy provider can reflow them after a resolution or orientation
change.

Three-button navigation binds each actual Tk label and returns `break`, so the
same release cannot execute again through the Toplevel bindtag. The Toplevel
three-zone handler remains as a release-only CH347 fallback. Every button and
committed pill gesture first uses the window-manager v1 typed entrypoint:

```text
navigation_action({action: "back" | "home" | "apps", input: "button" | "swipe"})
```

`navigate` is tried only when a provider explicitly reports that
`navigation_action` is unavailable. A provider that exposes neither typed
method receives the old `back`/`home`/`close_active`, task-switcher `show`, and
`recents` calls. Semantic failures and timeouts are never replayed through a
fallback, so one physical release cannot execute twice. Typed Home resolves
the current launcher role rather than a package id; typed Apps resolves the
current task-switcher role and its Recents callback. Those potentially
reentrant operations are owned by the X11 policy worker, while the shell also
keeps every broker round trip off Tk's event thread.

An inward pill swipe is typed Back with `input=swipe` and retains
`close_active` only for an old provider, so it exits the foreground app while
remaining compatible with overlay-aware Back policy. Holding the same inward
drag at least 28 pixels for 420 ms sends typed Apps with `input=swipe` and
opens the selected task-switcher role. The gesture latches after that dispatch:
continued motion and release cannot issue Back or Apps a second time. A touch
cancel or an orientation/edge change cancels the in-flight gesture, including
its later release. Bottom, top, left, and right navigation edges all define
inward relative to their current screen position; `MSYS_NAV_EDGE` may provide
an explicit policy edge when a window manager cannot report reliable geometry.

During motion the pill follows the finger by a bounded amount, stretches, and
blends toward an accent as the Recents threshold approaches. Release, cancel,
success, and failure ease it back with short `Tk.after()` frames. No sleep or
broker call runs on Tk's pointer thread. Failures flash the affected navigation
button (or pill) red for less than one second. They are logged in detail but
never create a notification toast or another overlay, so feedback cannot cover
the application or block another navigation action.

The pill provider draws only a centered 48x4 rounded light indicator on the
dark navigation edge (rotated to 4x48 on a side edge). It contains no debug or
button labels. The whole invisible surface still preserves the three-zone tap
fallback and the inward close gesture, so minimal presentation does not reduce
release-only touch compatibility.

The task switcher fetches its rows only from `role:window-manager.recents()`.
It presents up to four compact Material-like cards with title, stable identity,
live status, and explicit Open/Close actions, plus a deliberate empty state and
remaining-task count. Mapping and dismissal use a short timer-driven fade and
slide; unsupported X11 alpha gracefully retains the slide. All visible copy is
centralized behind `task_text()` so it has one seam for the shared i18n
translator rather than locale branches mixed into layout code. The validated
`files/share/i18n/catalog.json` supplies `en-US` plus a reusable `zh` base
catalog (also selected by `zh-CN`/`zh-Hans-CN` fallback) for Launcher,
navigation, recent tasks, notifications, intent chooser, screen shield and
transition surfaces. Locale selection follows `MSYS_LOCALE`, `LC_ALL`,
`LC_MESSAGES`, then `LANG`.

The right-most touch target in system chrome is a small keyboard glyph. It
calls only `role:input-method.toggle`; the selected provider remains
replaceable, while a downward chrome gesture continues to open notifications.
The window-policy Back path hides a visible input-method overlay before it
changes the foreground application.

Open sends the exact managed component through `msys.core.start`, whose normal
foreground transaction raises the existing window instead of spawning a second
generation. Close first makes that selected component foreground, temporarily
hides the Recents overlay, and then calls `role:window-manager.close_active`;
this prevents Back's required “dismiss Recents first” rule from consuming the
explicit Close action. The panel is destroyed only after a successful terminal
reply. A remote error or semantic `ok:false` restores the panel, re-enables its
buttons, and displays the error instead of looking like a successful no-op.
Raw unmanaged X11 rows remain visible for diagnostics but receive no fake
component lifecycle buttons.

The status agent advertises ready before polling HAL. Its cold
`org.msys.hal.manager.v1.inventory` request has a bounded 35-second budget so a
manager can start and describe a full provider catalog without being mistaken
for a provider failure; the selected device's normal `get_state` read remains
bounded to four seconds.

Toast payloads are normalized before reaching Tk. Invalid timeout values use
the configured default and all toasts are clamped to 0.5–6 seconds, scheduled
from the withdrawn host window, and remain tap-dismissable. Replacing or
dismissing a toast cancels its sole host timer, so stale timers cannot keep an
overlay alive or build up behind a busy notification source. Notification
history never opens the notification center by itself; the panel is mapped only
by an explicit `show`/`toggle` and Back can hide it through window policy.

`msys_shell_pyside.intent_chooser` is the reference graphical `chooser` role.
It displays registry-provided handlers for an ambiguous intent and can remember
a scheme-, MIME-, panel-, or action-scoped preference. Preferences are atomically
stored at `${MSYS_STATE_DIR:-/opt/msys-state}/preferences/intents.json`; set
`MSYS_CHOOSER_PREFERENCES` to override that file. The dialog defaults to 25
seconds (`MSYS_CHOOSER_TIMEOUT_MS`) and consumes the remaining end-to-end mIPC
caller deadline while reserving time to return the choice.

`msys_shell_pyside.notification_center` is the zero-external-dependency Tk
provider for the `notification-center` role. It subscribes to
`msys.role.notification-presenter` and `msys.notification.post`, persists a
bounded newest-first history, and implements the `show`, `hide`, `toggle`,
`list`, and `clear` mIPC methods. The panel starts hidden and never takes a
global grab. System chrome reuses its resident private mIPC channel for this
role call instead of opening another public control connection on every touch.
Long notification rows are wrapped to the live panel width and remain
vertically scrollable; intent copy and launcher status text use the same
responsive wrap policy on narrow screens. History defaults to 100 entries at
`${MSYS_STATE_DIR:-/opt/msys-state}/notifications/history.json`; override it
with `MSYS_NOTIFICATION_HISTORY` and set the bound with
`MSYS_NOTIFICATION_HISTORY_LIMIT` (maximum 1000).

The provider advertises component readiness before importing or initializing
Tk, so a slow first X11/font initialization cannot cross the supervisor's
five-second readiness deadline and cause a wasteful warm-process restart. An
early `show` request is accepted by the IPC worker and retained in the UI
queue. The panel itself remains lazy, maps as a light touch-sized surface, and
renders a bounded newest-first prefix before completing a large history at Tk
idle priority. If a product profile supplies the already-validated
`MSYS_UI_FONT_FAMILY`, the notification provider configures that exact family
without enumerating every Xft family; profiles without an explicit family keep
the SDK's CJK-capable discovery policy. The real managed notification
`Toplevel` receives the canonical package, component, role, and WM class
properties before its first map; those properties are not assumed to inherit
from the hidden Tk host. Set `MSYS_STARTUP_TIMING=1` (or `DEBUG=1`) to emit
one-shot module/mIPC/history/Tk/font/panel/first-show phase timings for cold
start diagnosis without keeping the provider resident.

The canonical manifest keeps every visual role on `windowing.display=inherit`
and declares its X11, mIPC subscription/call/publication, and private state
requirements explicitly. These declarations remain auditable policy metadata;
they do not introduce D-Bus, systemd, or a host package manager.
