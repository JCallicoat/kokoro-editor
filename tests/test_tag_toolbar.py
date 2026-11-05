"""The tag toolbar under the editor."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QInputDialog,
    QMessageBox,
)

from kokoro_editor.characters import Character
from kokoro_editor.settings import (
    load_casts,
    load_tag_toolbar_visible,
    save_cast,
)
from kokoro_editor.tag_toolbar import (
    CHARACTER_STRIP_MIN_WIDTH,
    SCROLL_STEP,
    PronunciationDialog,
    button_label,
)
from kokoro_editor.tags import (
    pause_tag,
    pronunciation_error,
    pronunciation_tag,
    rate_tag,
    tag_problems,
)


def names(window) -> list[str]:
    return [b.text() for b in window.tag_toolbar.strip.buttons]


def character_button(window, text: str):
    (found,) = [
        b for b in window.tag_toolbar.strip.buttons if b.text() == text
    ]
    return found


def button(window, text: str):
    (action,) = [a for a in window.tag_toolbar.actions() if a.text() == text]
    return action


def test_tag_formats_are_ones_the_server_understands():
    tags = [
        pause_tag(1.5),
        pause_tag(2.0),
        rate_tag(1.2),
        rate_tag(0.25),
        pronunciation_tag("Worcester", "wˈʊstər"),
    ]
    assert tags[:4] == [
        "[pause:1.5s]",
        "[pause:2s]",
        "[rate:1.2]",
        "[rate:0.25]",
    ]
    assert tag_problems(" ".join(tags)) == []


@pytest.mark.parametrize(
    ("word", "ipa", "error"),
    [
        ("Worcester", "wˈʊstər", None),
        ("", "x", "Enter the word to pronounce."),
        ("a[b]", "x", "The word can't contain square brackets."),
        ("word", " ", "Enter its pronunciation in IPA."),
        ("word", "a/b", "The IPA can't contain slashes or parentheses."),
    ],
)
def test_pronunciation_errors(word, ipa, error):
    assert pronunciation_error(word, ipa) == error


def test_buttons_follow_the_cast(window, saga):
    assert names(window) == ["No characters"]
    assert not window.tag_toolbar.strip.buttons[0].isEnabled()
    window.panel.load_cast("Saga")
    assert names(window) == ["narrator", "bob", "alice"]
    assert character_button(window, "bob").toolTip() == (
        "bob\nVoice: am_puck\nRate: 1.00×"
    )
    window.panel.set_characters([Character("eve")])  # as import/insert do
    assert names(window) == ["eve"]
    save_cast("Other", [Character("x"), Character("y")])
    window.panel.load_cast("Other")
    assert names(window) == ["x", "y"]
    tags = [a.text() for a in window.tag_toolbar.actions() if a.text()]
    assert tags == ["Pause…", "Rate…", "Pronunciation…"]


def test_character_button_inserts_its_tag(window, saga):
    window.panel.load_cast("Saga")
    window.text_edit.setPlainText("Hello")
    window.text_edit.moveCursor(
        window.text_edit.textCursor().MoveOperation.End
    )
    character_button(window, "bob").click()
    assert window.text_edit.toPlainText() == "Hello [voice:bob] "


def test_long_names_are_shortened_on_buttons(window):
    long_name = "a_very_long_character_x"  # 23 characters
    window.panel.set_characters([Character(long_name), Character("R&D")])
    assert names(window) == ["a_very_long_charact…", "R&&D"]
    assert len(button_label(long_name)) == 20
    assert (
        character_button(window, "a_very_long_charact…")
        .toolTip()
        .startswith(long_name)
    )


def strip_at(window, width: int):
    window.resize(width, 400)
    QApplication.instance().processEvents()
    return window.tag_toolbar.strip


def tag_buttons_visible(window) -> bool:
    toolbar = window.tag_toolbar
    return all(
        toolbar.widgetForAction(a).isVisible()
        for a in toolbar.actions()
        if a.text() in ("Pause…", "Rate…", "Pronunciation…")
    )


def test_strip_uses_the_room_it_has_and_scrolls_past_it(window):
    window.show()
    window.panel.set_characters(
        [Character(f"character_{n}") for n in range(12)]
    )
    strip = strip_at(window, 1800)  # room for every button
    bar = strip.scroll_area.horizontalScrollBar()
    assert strip.width() == strip.sizeHint().width()
    assert bar.maximum() == 0
    assert not strip.left_btn.isVisibleTo(strip)

    strip = strip_at(window, 600)  # not enough: it shrinks and scrolls
    assert strip.width() < strip.sizeHint().width()
    assert tag_buttons_visible(window)
    assert bar.maximum() > 0
    assert strip.left_btn.isVisibleTo(strip)
    assert not strip.left_btn.isEnabled() and strip.right_btn.isEnabled()
    strip.right_btn.click()
    assert bar.value() == SCROLL_STEP and strip.left_btn.isEnabled()
    bar.setValue(bar.maximum())
    assert not strip.right_btn.isEnabled()

    strip = strip_at(window, 1800)  # room again: no arrows
    assert bar.maximum() == 0
    assert not strip.right_btn.isVisibleTo(strip)


def test_strip_is_only_as_wide_as_a_short_cast(window):
    window.show()
    window.panel.set_characters([Character("al"), Character("bo")])
    strip = strip_at(window, 1200)
    assert strip.width() == strip.sizeHint().width()
    assert strip.width() < CHARACTER_STRIP_MIN_WIDTH


def answer_number(monkeypatch, value, ok=True):
    asked = []

    def get_double(parent, title, label, current, low, high, decimals):
        asked.append((title, current, low, high))
        return value, ok

    monkeypatch.setattr(QInputDialog, "getDouble", staticmethod(get_double))
    return asked


def test_pause_and_rate_ask_then_insert(window, monkeypatch):
    edit = window.text_edit
    asked = answer_number(monkeypatch, 1.5)
    button(window, "Pause…").trigger()
    assert edit.toPlainText() == "[pause:1.5s] "
    assert asked[0] == ("Insert Pause", 1.0, 0.05, 60.0)
    button(window, "Pause…").trigger()
    assert asked[1][1] == 1.5  # remembers the last length
    answer_number(monkeypatch, 0.9)
    edit.setPlainText("")
    button(window, "Rate…").trigger()
    assert edit.toPlainText() == "[rate:0.9] "


def test_cancelling_inserts_nothing(window, monkeypatch):
    answer_number(monkeypatch, 2.0, ok=False)
    window.text_edit.setPlainText("Text")
    button(window, "Pause…").trigger()
    button(window, "Rate…").trigger()
    assert window.text_edit.toPlainText() == "Text"


def answer_pronunciation(monkeypatch, ipa, word=None):
    seen = []

    def fake_exec(dialog):
        seen.append(dialog.word_edit.text())
        if word is not None:
            dialog.word_edit.setText(word)
        dialog.ipa_edit.setText(ipa)
        dialog.accept()
        return dialog.result()

    monkeypatch.setattr(PronunciationDialog, "exec", fake_exec)
    return seen


def test_pronunciation_replaces_the_selected_word(window, monkeypatch):
    edit = window.text_edit
    edit.setPlainText("Visit Worcester soon.")
    cursor = edit.textCursor()
    cursor.setPosition(6)
    cursor.setPosition(15, cursor.MoveMode.KeepAnchor)
    edit.setTextCursor(cursor)
    seen = answer_pronunciation(monkeypatch, "/wˈʊstər/")
    button(window, "Pronunciation…").trigger()
    assert seen == ["Worcester"]
    assert edit.toPlainText() == "Visit [Worcester](/wˈʊstər/) soon."


def test_pronunciation_without_a_selection(window, monkeypatch):
    window.text_edit.setPlainText("Say")
    window.text_edit.moveCursor(
        window.text_edit.textCursor().MoveOperation.End
    )
    answer_pronunciation(monkeypatch, "tomˈɑːto", word="tomato")
    button(window, "Pronunciation…").trigger()
    assert window.text_edit.toPlainText() == "Say [tomato](/tomˈɑːto/) "


def test_pronunciation_dialog_refuses_bad_input(qapp, boxes):
    dialog = PronunciationDialog("word", "")
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert boxes.last == "Enter its pronunciation in IPA."
    dialog.ipa_edit.setText("/ok/")
    assert dialog.values() == ("word", "ok")
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_toolbar_can_be_hidden_and_remembers(window):
    from kokoro_editor.main_window import MainWindow

    assert window.toolbar_action.shortcut().toString() == "Alt+="
    assert window.toolbar_action.isChecked()
    assert window.tag_toolbar.isVisibleTo(window)
    window.toolbar_action.trigger()
    assert not window.tag_toolbar.isVisibleTo(window)
    assert not load_tag_toolbar_visible()
    again = MainWindow()
    assert not again.toolbar_action.isChecked()
    assert not again.tag_toolbar.isVisibleTo(again)


def test_strip_follows_unsaved_cast_edits(window, boxes, monkeypatch):
    from kokoro_editor.characters_panel import CharacterDialog

    window.resize(1300, 400)
    window.show()
    panel, strip = window.panel, window.tag_toolbar.strip
    panel.set_characters([Character("al"), Character("bo")])
    queue = [Character("charlie_the_third"), Character("bo_renamed_longer")]

    def fake_exec(dialog):
        dialog.name_edit.setText(queue.pop(0).name)
        dialog.accept()
        return dialog.result()

    monkeypatch.setattr(CharacterDialog, "exec", fake_exec)
    widths = []

    def settle():
        QApplication.instance().processEvents()
        assert strip.width() == strip.sizeHint().width()
        # Everything fits: neither arrow (they once disagreed after an add).
        assert not strip.left_btn.isVisibleTo(strip)
        assert not strip.right_btn.isVisibleTo(strip)
        assert strip.scroll_area.horizontalScrollBar().maximum() == 0
        widths.append(strip.width())

    settle()
    panel._on_add()
    settle()
    panel._edit_row(1)
    settle()
    assert names(window) == ["al", "bo_renamed_longer", "charlie_the_third"]
    panel.table.selectRow(0)
    boxes.answers = [QMessageBox.StandardButton.Yes]
    panel._on_remove()
    settle()
    assert names(window) == ["bo_renamed_longer", "charlie_the_third"]
    assert widths[0] < widths[1] < widths[2]
    assert widths[3] < widths[2]
    assert panel.cast_name() is None  # never saved


def test_alt_s_saves_the_cast(window, boxes, saga):
    window.show()
    window.activateWindow()
    QTest.qWaitForWindowExposed(window)
    panel = window.panel
    panel.load_cast("Saga")
    panel.set_characters([Character("solo")])
    alt_s = (window.text_edit, Qt.Key.Key_S, Qt.KeyboardModifier.AltModifier)
    QTest.keyClick(*alt_s)
    assert load_casts()["Saga"] == [Character("solo")]
    assert not panel.cast_modified()
    assert window.status_label.text() == "Saved the cast Saga."
    # A cast not saved yet is asked for a name.
    panel._set_cast(None, [Character("new")])
    boxes.texts = [("Fresh", True)]
    QTest.keyClick(*alt_s)
    assert load_casts()["Fresh"] == [Character("new")]
    assert panel.cast_name() == "Fresh"
