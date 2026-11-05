"""Main window: inserting and renaming tags, checking them before
speaking, shortcuts and settings."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QMimeData, QPointF, Qt
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QMessageBox,
)

from kokoro_editor import characters_panel
from kokoro_editor.casts_tab import ConflictDialog
from kokoro_editor.characters import Character
from kokoro_editor.characters_panel import CharacterPickerDialog
from kokoro_editor.settings import save_cast
from kokoro_editor.settings_dialog import SettingsDialog

Btn = QMessageBox.StandardButton
NARRATOR = "[voice:narrator]"
VOICES = ["af_bella", "af_sky", "am_michael", "am_puck"]


@pytest.fixture
def shown(window, saga):
    """The window on screen and focused, with the Saga cast loaded and the
    server's voice list known."""
    window.panel.load_cast("Saga")
    window.panel._voices = list(VOICES)
    window.show()
    window.activateWindow()
    QTest.qWaitForWindowExposed(window)
    return window


def set_text(window, text: str, cursor: int, anchor: int | None = None):
    edit = window.text_edit
    edit.setPlainText(text)
    c = edit.textCursor()
    c.setPosition(cursor if anchor is None else anchor)
    if anchor is not None:
        c.setPosition(cursor, c.MoveMode.KeepAnchor)
    edit.setTextCursor(c)


@pytest.mark.parametrize(
    ("text", "cursor", "anchor", "expected"),
    [
        ("", 0, None, f"{NARRATOR} "),
        ("Hello", 0, None, f"{NARRATOR} Hello"),
        ("Hello", 5, None, f"Hello {NARRATOR} "),
        ("Hello world", 6, None, f"Hello {NARRATOR} world"),
        ("ab", 1, None, f"a {NARRATOR} b"),
        ("one\ntwo", 4, None, f"one\n{NARRATOR} two"),
        ("one\ntwo", 3, None, f"one {NARRATOR} \ntwo"),
        ("Hello world", 11, 6, f"Hello {NARRATOR} world"),  # selection kept
    ],
)
def test_insert_voice_tag_spacing(window, text, cursor, anchor, expected):
    set_text(window, text, cursor, anchor)
    window.insert_voice_tag(Character("narrator"))
    assert window.text_edit.toPlainText() == expected


def test_renaming_a_character_renames_its_tags(window):
    edit = window.text_edit
    text = (
        "😀 [voice:bob] Hi. [voice: BOB ] Yo [voice:bobby] "
        "[voice:bob+af_sky] [voice:Bob]\n[voice:bob]"
    )
    edit.setPlainText(text)
    window.panel.character_renamed.emit("bob", "robert")
    assert edit.toPlainText() == (
        "😀 [voice:robert] Hi. [voice:robert] Yo [voice:bobby] "
        "[voice:bob+af_sky] [voice:robert]\n[voice:robert]"
    )
    assert window.status_label.text() == "Renamed 4 tags from bob to robert."
    edit.undo()  # one undo step
    assert edit.toPlainText() == text


def test_dropping_a_character_inserts_its_tag(shown):
    edit = shown.text_edit
    shown.panel.set_collapsed(False)
    data = shown.panel.table.mimeData([shown.panel.table.item(1, 0)])
    set_text(shown, "Hello world", 0)
    c = edit.textCursor()
    c.setPosition(6)
    pos = QPointF(edit.cursorRect(c).center())
    viewport = edit.viewport()
    args = (data, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    for event_type in (QDragEnterEvent, QDragMoveEvent):
        QApplication.sendEvent(
            viewport,
            event_type(pos.toPoint(), Qt.DropAction.CopyAction, *args),
        )
    QApplication.sendEvent(
        viewport, QDropEvent(pos, Qt.DropAction.CopyAction, *args)
    )
    assert edit.toPlainText() == "Hello [voice:bob] world"
    plain = QMimeData()
    plain.setText("XY")
    edit.insertFromMimeData(plain)  # ordinary text drops are unchanged
    assert "XY" in edit.toPlainText()


def alt(window, key):
    QTest.keyClick(window.text_edit, key, Qt.KeyboardModifier.AltModifier)


def test_alt_digits_insert_characters(shown):
    edit = shown.text_edit
    edit.setFocus()
    alt(shown, Qt.Key.Key_2)
    assert edit.toPlainText() == "[voice:bob] "
    alt(shown, Qt.Key.Key_5)  # no fifth character: the digit isn't typed
    assert edit.toPlainText() == "[voice:bob] "
    assert shown.status_label.text() == "No character 5 in this cast."
    labels = [a.text() for a in shown.insert_tag_menu.actions()]
    assert labels[:3] == [
        "&1  narrator\tAlt+1",
        "&2  bob\tAlt+2",
        "&3  alice\tAlt+3",
    ]


def test_alt_0_pick_from_another_cast_switches_to_it(
    shown, boxes, monkeypatch
):
    save_cast("Other", [Character("eve", "af_bella")])
    shown.panel.set_characters([Character("changed")])

    def pick_eve(dialog):
        dialog.cast_box.setCurrentText("Other")
        dialog.character_list.setCurrentRow(0)
        dialog.accept()
        return dialog.result()

    monkeypatch.setattr(CharacterPickerDialog, "exec", pick_eve)
    shown.text_edit.clear()
    boxes.answers = [Btn.Discard]
    alt(shown, Qt.Key.Key_0)
    assert shown.panel.cast_name() == "Other"
    assert shown.text_edit.toPlainText() == "[voice:eve] "


def test_alt_minus_toggles_the_panel(shown):
    alt(shown, Qt.Key.Key_Minus)
    assert not shown.panel.is_collapsed() and shown.panel.table.hasFocus()
    assert shown.panel_action.isChecked()
    QTest.keyClick(
        shown.panel.table, Qt.Key.Key_Minus, Qt.KeyboardModifier.AltModifier
    )
    assert shown.panel.is_collapsed() and shown.text_edit.hasFocus()


def test_preview_pauses_document_playback_and_resumes_it(window):
    class Player:
        paused = False

        def set_paused(self, paused):
            self.paused = paused

    window._player = player = Player()
    window._on_preview_active(True)
    assert player.paused
    window._on_preview_active(False)
    assert not player.paused
    player.paused = True  # paused by the user: stays paused
    window._on_preview_active(True)
    window._on_preview_active(False)
    assert player.paused
    window._player = None


# -- checking tags before speaking -------------------------------------- #
def picker_answers(monkeypatch, *picks):
    """Answer each replacement picker with a list of (cast, row) attempts,
    the next tried while it stays open; None skips."""
    queue, seen = list(picks), []

    def fake_exec(dialog):
        seen.append(dialog.findChildren(characters_panel.QLabel)[0].text())
        for attempt in queue.pop(0):
            if attempt is None:
                return QDialog.DialogCode.Rejected
            cast, row = attempt
            dialog.cast_box.setCurrentText(cast)
            dialog.character_list.setCurrentRow(row)
            dialog.accept()
            if dialog.result() == QDialog.DialogCode.Accepted:
                return dialog.result()
        raise AssertionError("the picker never closed")

    monkeypatch.setattr(CharacterPickerDialog, "exec", fake_exec)
    return seen


def test_known_tags_are_sent_as_they_are(shown, boxes):
    text = f"{NARRATOR} Hi. [voice:am_michael(3)+af_bella(1)] Yo."
    shown.text_edit.setPlainText(text)
    assert shown._prepare_speech() == text
    assert boxes.messages == []


def test_unknown_tags_left_out_of_what_is_sent(shown, boxes):
    text = f"{NARRATOR} Hi. [voice:ghost] Boo! [voice:bob] Yo."
    shown.text_edit.setPlainText(text)
    boxes.answers = [Btn.No]
    assert shown._prepare_speech() == f"{NARRATOR} Hi. Boo! [voice:bob] Yo."
    assert shown.text_edit.toPlainText() == text
    assert "[voice:ghost]" in boxes.last
    assert "left out of what's sent" in boxes.last


def test_nothing_left_to_speak(shown, boxes):
    shown.text_edit.setPlainText("[voice:ghost]")
    boxes.answers = [Btn.No]
    assert shown._prepare_speech() is None
    assert shown.status_label.text() == "Nothing to speak."


def test_replacing_unknown_tags(shown, boxes, monkeypatch):
    save_cast(
        "Other", [Character("eve", "af_bella"), Character("Bob", "am_michael")]
    )
    shown.text_edit.setPlainText(
        "😀 [voice:ghost] Boo! [voice:Ghost] again. [voice:wraith] Ooh. "
        "[voice:imp] Hee. [voice:sprite] Skip me."
    )
    boxes.answers = [
        Btn.Yes,  # replace the unknown tags?
        Btn.No,  # ghost -> eve from Other: import? No, the picker stays
        Btn.Yes,  # wraith -> eve: import
        Btn.Yes,  # imp -> Bob: import, clashing with bob
    ]
    seen = picker_answers(
        monkeypatch,
        [("Other", 0), ("Saga", 0)],  # ghost: eve declined, then narrator
        [("Other", 0)],  # wraith: eve
        [("Other", 1)],  # imp: Bob, renamed on import
        [None],  # sprite: skipped
    )

    def rename(dialog):
        assert not dialog.apply_all_check.isVisibleTo(dialog)
        dialog.rename_check.setChecked(True)
        dialog.rename_edit.setText("bob_other")
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ConflictDialog, "exec", rename)
    sent = shown._prepare_speech()
    assert shown.text_edit.toPlainText() == (
        "😀 [voice:narrator] Boo! [voice:narrator] again. [voice:eve] Ooh. "
        "[voice:bob_other] Hee. [voice:sprite] Skip me."
    )
    assert sent == (
        "😀 [voice:narrator] Boo! [voice:narrator] again. [voice:eve] Ooh. "
        "[voice:bob_other] Hee. Skip me."
    )
    assert seen[0] == "Replace every [voice:ghost] in the document with:"
    assert (
        "eve is in the cast Other. Import it into Saga so its tags can be "
        "spoken?"
    ) in boxes.texts_shown
    panel = shown.panel
    assert [c.name for c in panel.characters()][-2:] == ["eve", "bob_other"]
    assert panel.character_named("bob_other").voice == "am_michael"
    assert panel.cast_modified()


def test_keeping_the_existing_character_on_import(shown, boxes, monkeypatch):
    save_cast("Other", [Character("Bob", "am_michael")])
    shown.text_edit.setPlainText("[voice:ghost] x")
    boxes.answers = [Btn.Yes, Btn.Yes]
    picker_answers(monkeypatch, [("Other", 0)])
    monkeypatch.setattr(
        ConflictDialog, "exec", lambda _d: QDialog.DialogCode.Accepted
    )
    shown._prepare_speech()
    assert shown.text_edit.toPlainText() == "[voice:bob] x"


def test_characters_without_a_voice_stop_speaking(shown, boxes):
    shown.panel.set_characters([Character("mute")])
    shown.text_edit.setPlainText("[voice:mute] hmm")
    assert shown._prepare_speech() is None
    assert boxes.last.startswith("Choose a voice for these characters")


def test_validate_document_tags(shown, boxes, monkeypatch):
    edit = shown.text_edit
    edit.setPlainText(f"{NARRATOR} fine [voice:am_michael(3)+af_bella(1)]")
    shown._validate_tags()
    assert boxes.last == "All voice tags are valid."
    edit.setPlainText("[voice:ghost] [voice:imp]")
    boxes.answers = [Btn.Yes]
    picker_answers(monkeypatch, [("Saga", 0)], [None])
    shown._validate_tags()
    assert "left as they are" in boxes.texts_shown[-1]
    assert edit.toPlainText() == f"{NARRATOR} [voice:imp]"
    assert shown.status_label.text() == (
        "1 unknown voice tag name(s) left: imp"
    )


# -- settings dialog ---------------------------------------------------- #
def test_settings_tabs_switch_with_alt_and_first_letter(window):
    dialog = SettingsDialog(
        window.settings, window._nam, lambda _a: None, window.panel, window
    )
    box = dialog.findChildren(QDialogButtonBox)[-1]
    cancel = box.button(QDialogButtonBox.StandardButton.Cancel)
    assert "&" not in cancel.text()  # Alt+C belongs to the Casts tab
    dialog.show()
    dialog.activateWindow()
    QTest.qWaitForWindowExposed(dialog)
    tabs = []
    for key in (Qt.Key.Key_C, Qt.Key.Key_K, Qt.Key.Key_E):
        QTest.keyClick(dialog, key, Qt.KeyboardModifier.AltModifier)
        tabs.append(dialog.tabs.tabText(dialog.tabs.currentIndex()))
    assert tabs == ["&Casts", "&Kokoro", "&Editor"]
    assert dialog.isVisible()
    dialog.reject()
