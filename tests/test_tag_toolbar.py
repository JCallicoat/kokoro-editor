"""The tag toolbar under the editor."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import (
    QDrag,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QGuiApplication,
    QHelpEvent,
)
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QInputDialog,
    QMessageBox,
    QToolTip,
)

from kokoro_editor.characters import CHARACTER_MIME_TYPE, Character
from kokoro_editor.colors import text_color
from kokoro_editor.settings import (
    load_casts,
    load_tag_toolbar_visible,
    save_cast,
)
from kokoro_editor.tag_toolbar import (
    BUTTON_SPACING,
    CHARACTER_STRIP_MIN_WIDTH,
    SCROLL_STEP,
    PronunciationDialog,
    button_label,
    new_tag_mime_data,
)
from kokoro_editor.tags import (
    pause_tag,
    pronunciation_error,
    pronunciation_tag,
    rate_tag,
    tag_problems,
)
from kokoro_editor.widgets import (
    NEW_TAG_MIME_TYPE,
    ShiftTooltips,
    rich_tooltip,
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

    def get_double(parent, title, label, current, low, high, decimals, **kw):
        asked.append((title, current, low, high, kw.get("step", 1)))
        return value, ok

    monkeypatch.setattr(QInputDialog, "getDouble", staticmethod(get_double))
    return asked


def test_pause_and_rate_ask_then_insert(window, monkeypatch):
    edit = window.text_edit
    asked = answer_number(monkeypatch, 1.5)
    button(window, "Pause…").trigger()
    assert edit.toPlainText() == "[pause:1.5s] "
    assert asked[0] == ("Insert Pause", 1.0, 0.05, 60.0, 1)
    button(window, "Pause…").trigger()
    assert asked[1][1] == 1.5  # remembers the last length
    edit.setPlainText("")
    asked = answer_number(monkeypatch, 0.9)
    button(window, "Rate…").trigger()
    assert edit.toPlainText() == "[rate:0.9] "
    assert asked[0][-1] == 0.05  # the arrows step finely


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


def test_tag_buttons_are_in_their_tag_colours(window):
    buttons = window.tag_toolbar.tag_buttons
    for kind in ("pause", "rate", "pronunciation"):
        color = getattr(window.settings, f"{kind}_color")
        assert f"background-color: {color};" in buttons[kind].styleSheet()
        assert f"color: {text_color(color)};" in buttons[kind].styleSheet()
    window.settings.pause_color = "#123456"  # as Settings would
    window._apply_settings()
    assert "#123456" in buttons["pause"].styleSheet()


def test_buttons_are_plain_without_highlighting(window, saga):
    from kokoro_editor.main_window import MainWindow

    window.panel.load_cast("Saga")
    toolbar = window.tag_toolbar
    buttons = [*toolbar.strip.buttons, *toolbar.tag_buttons.values()]
    assert all(b.styleSheet() for b in buttons)
    window.highlight_action.trigger()  # off
    assert not any(b.styleSheet() for b in buttons)
    window.panel.set_characters([Character("eve")])  # still plain
    assert not character_button(window, "eve").styleSheet()
    window.settings.pause_color = "#123456"
    window._apply_settings()
    assert not toolbar.tag_buttons["pause"].styleSheet()
    again = MainWindow()  # remembered
    assert not again.tag_toolbar.tag_buttons["pause"].styleSheet()
    window.highlight_action.trigger()  # on again
    assert "#123456" in toolbar.tag_buttons["pause"].styleSheet()
    assert character_button(window, "eve").styleSheet()


def test_all_buttons_are_spaced_alike(window):
    window.show()
    window.panel.set_characters([Character("al"), Character("bo")])
    QApplication.instance().processEvents()
    toolbar = window.tag_toolbar

    def gap(left, right):
        start = right.mapTo(toolbar, QPoint(0, 0)).x()
        return start - left.mapTo(toolbar, QPoint(left.width(), 0)).x()

    al, bo = toolbar.strip.buttons
    pause, rate, pronunciation = toolbar.tag_buttons.values()
    assert gap(al, bo) == BUTTON_SPACING
    assert gap(pause, rate) == gap(rate, pronunciation) == BUTTON_SPACING
    window.tag_toolbar.setStyle(window.tag_toolbar.style())  # a new style
    assert toolbar.layout().spacing() == BUTTON_SPACING


def drag_button(button, monkeypatch):
    """Press button and move far enough to drag it: what it would drag,
    and whether it was clicked."""
    dragged, clicked = [], []

    def fake_exec(drag, *args):
        dragged.append(drag.mimeData())
        return Qt.DropAction.IgnoreAction

    monkeypatch.setattr(QDrag, "exec", fake_exec)
    button.clicked.connect(lambda: clicked.append(True))
    start = QPoint(5, 5)
    far = start + QPoint(QApplication.startDragDistance() + 5, 0)
    QTest.mousePress(button, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(button, far)
    QTest.mouseRelease(button, Qt.MouseButton.LeftButton, pos=far)
    return dragged, clicked


def test_character_buttons_can_be_dragged(window, saga, monkeypatch):
    window.show()
    window.panel.load_cast("Saga")
    dragged, clicked = drag_button(
        character_button(window, "bob"), monkeypatch
    )
    (data,) = dragged
    assert bytes(data.data(CHARACTER_MIME_TYPE).data()) == b"bob"
    assert data.text() == "[voice:bob]"
    assert not clicked


def test_tag_buttons_can_be_dragged(window, monkeypatch):
    window.show()
    button = window.tag_toolbar.tag_buttons["rate"]
    dragged, clicked = drag_button(button, monkeypatch)
    (data,) = dragged
    assert bytes(data.data(NEW_TAG_MIME_TYPE).data()) == b"rate"
    assert not clicked


def drop_on_editor(window, data, position):
    edit = window.text_edit
    cursor = edit.textCursor()
    cursor.setPosition(position)
    pos = QPointF(edit.cursorRect(cursor).center())
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
    QApplication.instance().processEvents()  # the queued question


def test_dropped_tag_buttons_ask_then_insert_there(window, monkeypatch):
    window.show()
    edit = window.text_edit
    edit.setPlainText("Hello world")
    asked = answer_number(monkeypatch, 2.0)
    drop_on_editor(window, new_tag_mime_data("pause"), 6)
    assert asked[0][0] == "Insert Pause"
    assert edit.toPlainText() == "Hello [pause:2s] world"
    answer_pronunciation(monkeypatch, "tomˈɑːto", word="tomato")
    drop_on_editor(window, new_tag_mime_data("pronunciation"), 0)
    assert edit.toPlainText() == "[tomato](/tomˈɑːto/) Hello [pause:2s] world"
    answer_number(monkeypatch, 1.0, ok=False)  # cancelled: nothing
    drop_on_editor(window, new_tag_mime_data("rate"), 0)
    assert edit.toPlainText() == "[tomato](/tomˈɑːto/) Hello [pause:2s] world"


def test_strip_fits_after_toggling_colours(window):
    window.resize(1300, 400)
    window.show()
    strip = window.tag_toolbar.strip
    window.panel.set_characters(
        [Character("al"), Character("bo"), Character("charlie")]
    )

    def settle():
        QApplication.instance().processEvents()
        assert strip.width() == strip.sizeHint().width()
        assert not strip.left_btn.isVisibleTo(strip)
        assert not strip.right_btn.isVisibleTo(strip)
        assert strip.scroll_area.horizontalScrollBar().maximum() == 0
        heights = [b.sizeHint().height() for b in strip.buttons]
        assert strip.scroll_area.height() >= max(heights)
        return strip.width()

    colored = settle()
    for _ in range(2):
        window.highlight_action.trigger()  # off
        plain = settle()
        window.highlight_action.trigger()  # on
        assert settle() == colored
    assert plain != colored


@pytest.fixture
def shift_tooltips(qapp, monkeypatch):
    """ShiftTooltips installed, as the app does, and a Shift key that
    shift(True/False) holds down or lets go."""
    tooltips = ShiftTooltips(qapp)
    held = [False]
    no, yes = Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier
    monkeypatch.setattr(
        QGuiApplication,
        "queryKeyboardModifiers",
        staticmethod(lambda: yes if held[0] else no),
    )

    def shift(down: bool, widget=None) -> None:
        held[0] = down
        if widget is not None:
            send = QTest.keyPress if down else QTest.keyRelease
            send(widget, Qt.Key.Key_Shift)

    yield shift
    qapp.removeEventFilter(tooltips)
    tooltips.setParent(None)
    QToolTip.hideText()


def hover(widget, pos: QPoint) -> str:
    event = QHelpEvent(QEvent.Type.ToolTip, pos, widget.mapToGlobal(pos))
    QApplication.sendEvent(widget, event)
    return QToolTip.text()


def test_shift_shows_a_characters_description(window, shift_tooltips):
    window.show()
    bob = Character("bob", "am_puck", description="Gruff.\n\nA sailor.")
    window.panel.set_characters([bob, Character("eve")])
    button = character_button(window, "bob")
    assert hover(button, QPoint(5, 5)) == bob.tooltip()
    shift_tooltips(True, window)  # pressed while it shows
    assert QToolTip.text() == rich_tooltip(bob.whats_this())
    assert "<p>Gruff.</p><p>A sailor.</p>" in QToolTip.text()
    shift_tooltips(False, window)
    assert QToolTip.text() == bob.tooltip()
    # Already held when hovering: in the panel's table too.
    table = window.panel.table
    window.panel.set_collapsed(False)
    QApplication.instance().processEvents()
    centre = table.visualItemRect(table.item(0, 1)).center()
    assert hover(table.viewport(), centre) == bob.tooltip()
    shift_tooltips(True)
    assert hover(table.viewport(), centre) == rich_tooltip(bob.whats_this())
    # Nothing more to show for a character without one.
    eve = character_button(window, "eve")
    assert hover(eve, QPoint(5, 5)) == Character("eve").tooltip()


def test_shift_shows_a_description_over_a_tag(window, shift_tooltips):
    window.panel.set_characters([Character("bob", description="Gruff.")])
    text = "[voice:bob] Hi"
    assert window._tag_tooltip(text, 3).startswith(
        "bob\nVoice: (none)\nRate: 1.00×\nHold Shift for the description."
    )
    shift_tooltips(True)
    tip = window._tag_tooltip(text, 3)
    assert tip.startswith("<p>bob<br>")
    assert "<p>Gruff.</p><p>Double-click to change it" in tip
