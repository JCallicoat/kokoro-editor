"""Application entry point."""

from __future__ import annotations

import sys

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from kokoro_editor import __version__
from kokoro_editor.main_window import MainWindow
from kokoro_editor.settings import load_last_cast, load_recent_files


def main() -> int:
    QCoreApplication.setOrganizationName("KokoroEditor")
    QCoreApplication.setApplicationName("KokoroEditor")
    QCoreApplication.setApplicationVersion(__version__)
    # No setApplicationDisplayName(): Qt would append it to the window title,
    # which already starts with the app name.
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    args = app.arguments()[1:]
    restore = window.settings.restore_session
    # The cast is restored on its own, whether or not the file opens.
    if restore:
        window.panel.load_cast(load_last_cast())
    if args:
        window.open_path(args[0])
    elif restore:
        files = load_recent_files()
        if files:
            try:
                window.open_path(files[0])
            except Exception:
                window.status_label.setText("New Session")
    return app.exec()
