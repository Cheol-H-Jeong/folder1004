"""Application icon helpers for source and PyInstaller bundles."""
from __future__ import annotations

import ctypes
import sys
from pathlib import Path

from PySide6 import QtGui


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return base / relative


def icon_path() -> Path | None:
    for rel in ("assets/icon.ico", "assets/icon.png"):
        p = resource_path(rel)
        if p.exists():
            return p
    return None


def app_icon() -> QtGui.QIcon:
    p = icon_path()
    return QtGui.QIcon(str(p)) if p is not None else QtGui.QIcon()


def set_windows_app_user_model_id() -> None:
    """Give Windows taskbar a stable app id so the icon is not generic."""
    if not sys.platform.startswith("win"):
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(  # type: ignore[attr-defined]
            "Folder1004.Desktop.App"
        )
    except Exception:
        pass
