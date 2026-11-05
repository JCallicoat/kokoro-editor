"""Shared fixtures: an offscreen QApplication, empty settings for every
test, no network, and scripted answers for message and input boxes."""

from __future__ import annotations

import os
import uuid

# Must be set before Qt creates the application.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QSettings
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QInputDialog,
    QMessageBox,
)

from kokoro_editor import characters_panel, settings_dialog
from kokoro_editor.characters import Character
from kokoro_editor.settings import save_cast


def _delete_pending() -> None:
    """Run the deferred deletes queued by deleteLater()."""
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture(scope="session")
def qapp(tmp_path_factory):
    # Settings go under a temporary directory, never the user's own.
    QSettings.setPath(
        QSettings.Format.NativeFormat,
        QSettings.Scope.UserScope,
        str(tmp_path_factory.mktemp("settings")),
    )
    app = QApplication.instance() or QApplication([])
    yield app
    # Destroy every window while the application still exists; leaving them
    # to Python's garbage collector at exit crashes Qt. deleteLater() rather
    # than close() so no close prompts (unsaved changes) can appear.
    for widget in QApplication.topLevelWidgets():
        widget.deleteLater()
    _delete_pending()


@pytest.fixture(autouse=True)
def isolated_settings(qapp):
    """A fresh, empty settings store for each test, and its windows freed
    afterwards."""
    QCoreApplication.setOrganizationName(f"KokoroEditorTest-{uuid.uuid4()}")
    QCoreApplication.setApplicationName("KokoroEditor")
    yield
    for widget in QApplication.topLevelWidgets():
        widget.hide()
        widget.deleteLater()
    _delete_pending()


@pytest.fixture(autouse=True)
def no_unscripted_dialogs(monkeypatch):
    """Fail at once, instead of hanging, if a test opens a modal dialog it
    hasn't scripted (tests script one by patching its class's exec)."""

    def fail(dialog):
        pytest.fail(f"Unscripted dialog opened: {dialog.windowTitle()!r}")

    monkeypatch.setattr(QDialog, "exec", fail)


@pytest.fixture(autouse=True)
def no_voice_fetch(monkeypatch):
    """Keep tests off the network: nothing asks a server for its voices."""
    monkeypatch.setattr(
        characters_panel.PanelWidget, "_fetch_voices", lambda self: None
    )
    monkeypatch.setattr(
        settings_dialog.SettingsDialog, "_fetch_voices", lambda self: None
    )


class ScriptedBoxes:
    """Stands in for QMessageBox's static functions and
    QInputDialog.getText. Every message is recorded; answers are taken from
    the queues, falling back to the box's default button (or Ok)."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []  # kind, title, text
        self.answers: list[QMessageBox.StandardButton] = []
        self.texts: list[tuple[str, bool]] = []

    @property
    def texts_shown(self) -> list[str]:
        return [text for _kind, _title, text in self.messages]

    @property
    def last(self) -> str:
        return self.messages[-1][2]

    def box(self, kind: str):
        def show(parent, title, text, buttons=None, default=None):
            self.messages.append((kind, title, text))
            if self.answers:
                return self.answers.pop(0)
            if default not in (None, QMessageBox.StandardButton.NoButton):
                return default
            return QMessageBox.StandardButton.Ok

        return staticmethod(show)

    def get_text(self, *args, **kwargs):
        assert self.texts, "unexpected input dialog"
        return self.texts.pop(0)


@pytest.fixture
def boxes(monkeypatch) -> ScriptedBoxes:
    scripted = ScriptedBoxes()
    for kind in ("question", "warning", "information", "critical"):
        monkeypatch.setattr(QMessageBox, kind, scripted.box(kind))
    monkeypatch.setattr(
        QInputDialog, "getText", staticmethod(scripted.get_text)
    )
    return scripted


@pytest.fixture
def window(qapp, boxes):
    """A main window with a voice set, so Play and Render may proceed."""
    from kokoro_editor.main_window import MainWindow

    main = MainWindow()
    main.settings.voices = ["af_sky", "", ""]
    return main


@pytest.fixture
def saga():
    """A saved cast called Saga."""
    characters = [
        Character("narrator", "af_sky(2)+am_puck(1)", 0.8),
        Character("bob", "am_puck"),
        Character("alice", "af_bella"),
    ]
    save_cast("Saga", characters)
    return characters
