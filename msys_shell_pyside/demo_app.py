from __future__ import annotations

import os
import sys
import time

from .localization import shell_text
from msys_sdk.ui_fonts import configure_qt_fonts, configure_tk_fonts, font_spec


def run_headless() -> int:
    print(f"msys demo app headless DISPLAY={os.environ.get('DISPLAY', '')}", flush=True)
    while True:
        time.sleep(60)


def run_pyside() -> int:
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication(sys.argv)
    configure_qt_fonts(app, QtGui)
    app.setApplicationName(os.environ.get("MSYS_WINDOW_IDENTITY", "org.msys.demo.app"))
    if hasattr(app, "setDesktopFileName"):
        app.setDesktopFileName(os.environ.get("MSYS_WINDOW_IDENTITY", "org.msys.demo.app"))
    window = QtWidgets.QWidget()
    window.setWindowTitle(shell_text("demo.window_title"))
    layout = QtWidgets.QVBoxLayout(window)
    title = QtWidgets.QLabel(shell_text("demo.title"))
    title.setAlignment(QtCore.Qt.AlignCenter)
    body = QtWidgets.QLabel(f"DISPLAY={os.environ.get('DISPLAY', '')}")
    body.setAlignment(QtCore.Qt.AlignCenter)
    layout.addWidget(title)
    layout.addWidget(body)
    window.resize(300, 420)
    window.show()
    return app.exec()


def run_tk() -> int:
    import tkinter as tk

    identity = os.environ.get("MSYS_WINDOW_IDENTITY", "org.msys.demo.app")
    root = tk.Tk(className=identity)
    configure_tk_fonts(root, default_size=10)
    root.title(os.environ.get("MSYS_WINDOW_TITLE", shell_text("demo.window_title")))
    root.configure(bg="#18212b")
    root.geometry("300x420+10+30")
    root.resizable(True, True)
    title = tk.Label(
        root,
        text=shell_text("demo.title"),
        bg="#18212b",
        fg="white",
        font=font_spec(root, 20, "bold"),
    )
    title.pack(expand=True, fill="both", padx=18, pady=(30, 8))
    body = tk.Label(
        root,
        text=shell_text("demo.body", display=os.environ.get("DISPLAY", "")),
        bg="#18212b",
        fg="#9fb3c8",
        font=font_spec(root, 11),
        justify="center",
    )
    body.pack(expand=True, fill="both", padx=18, pady=(8, 30))
    root.mainloop()
    return 0


def main() -> int:
    if os.environ.get("MSYS_SHELL_HEADLESS") == "1":
        return run_headless()
    try:
        return run_pyside()
    except Exception as exc:
        print(f"demo app falling back from PySide to Tk: {exc}", flush=True)
    try:
        return run_tk()
    except Exception as exc:
        print(f"demo app falling back to headless: {exc}", flush=True)
        return run_headless()


if __name__ == "__main__":
    raise SystemExit(main())
