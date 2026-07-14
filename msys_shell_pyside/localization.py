"""Shared visible copy for every provider in the reference shell package."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from msys_sdk import Translator


SHELL_COPY: dict[str, str] = {
    "package.name": "MSYS Reference Shell",
    "package.summary": "Replaceable responsive shell, navigation, notification, chooser, and transition roles",
    "launcher.window_title": "MSYS Launcher",
    "launcher.title.mobile": "MSYS Apps",
    "launcher.title.desktop": "MSYS Desktop",
    "launcher.refresh": "Refresh",
    "launcher.ready": "Ready",
    "launcher.empty": "No launchable applications",
    "launcher.launch": "Launch",
    "launcher.starting": "Starting {name}…",
    "launcher.started": "Started {name}",
    "launcher.start_failed": "Start failed: {message}",
    "launcher.refreshing": "Refreshing applications…",
    "launcher.refresh_failed": "Refresh failed: {message}",
    "launcher.apps.one": "1 application",
    "launcher.apps.many": "{count} applications",
    "notification.window_title": "MSYS Notification Center",
    "notification.title": "Notifications",
    "notification.clear": "Clear",
    "notification.close": "Close",
    "notification.initial": "Notifications",
    "notification.empty": "No notifications",
    "notification.fallback": "Notification",
    "notification.entry.title": "{time}  {title}: {message}",
    "notification.entry.message": "{time}  {message}",
    "notification.entry.source": "{line}  [{source}]",
    "chooser.window_title": "MSYS Intent Chooser",
    "chooser.remember": "Remember for this type of request",
    "chooser.cancel": "Cancel",
    "chooser.open": "Open",
    "chooser.open_uri": "Open {scheme} link with",
    "chooser.open_mime": "Open {mime} with",
    "chooser.open_settings": "Open {name} settings with",
    "chooser.handle": "Handle {action} with",
    "chooser.countdown.seconds": "{seconds}s",
    "chooser.target.link": "link",
    "chooser.target.file": "file",
    "chooser.target.settings": "settings",
    "chooser.target.request": "request",
    "shield.window_title": "MSYS Screen Shield",
    "shield.message": "MSYS\nScreen shield",
    "transition.window_title": "MSYS Transition",
    "transition.opening": "Opening {title}",
    "transition.closing": "Closing {title}",
    "transition.open_failed": "Could not open {title}",
    "transition.preparing": "Preparing the application",
    "transition.returning": "Returning to the desktop",
    "transition.reported_failure": "The application reported a failure",
    "transition.failed": "Application failed",
    "transition.application": "Application",
    "chrome.window_title": "MSYS Chrome",
    "chrome.initial": "MSYS  |  :24  |  ready",
    "chrome.status": "MSYS  |  {time}",
    "chrome.status.battery": "MSYS  |  {time}  |  BAT {capacity}%",
    "demo.window_title": "MSYS Demo App",
    "demo.title": "MSYS Demo",
    "demo.body": "A normal manifest application\nDISPLAY={display}",
    "toast.window_title": "MSYS Notifications",
    "navigation.window_title": "MSYS Navigation",
}


SHELL_I18N = Translator.from_file(
    Path(__file__).resolve().parents[1] / "files" / "share" / "i18n" / "catalog.json"
)


def shell_text(
    key: str,
    params: Mapping[str, object] | None = None,
    *,
    fallback: str | None = None,
    **values: object,
) -> str:
    merged = dict(params or {})
    merged.update(values)
    english = fallback if fallback is not None else SHELL_COPY.get(key, key)
    return str(SHELL_I18N.text(key, merged, fallback=english))


__all__ = ["SHELL_COPY", "SHELL_I18N", "shell_text"]
