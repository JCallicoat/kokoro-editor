"""Tags in the text: highlighting, tooltips, double-click editing and
dragging, with and without spaces around them."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QByteArray, QMimeData, QPoint, QPointF, Qt
from PySide6.QtGui import QDrag, QDropEvent, QMouseEvent, QTextCharFormat
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QInputDialog,
    QMessageBox,
    QPushButton,
)

from kokoro_editor.characters import Character
from kokoro_editor.characters_panel import CharacterPickerDialog
from kokoro_editor.colors import DEFAULT_PAUSE_COLOR, text_color
from kokoro_editor.settings import (
    AppSettings,
    load_highlight_tags,
    save_cast,
)
from kokoro_editor.settings_dialog import SettingsDialog
from kokoro_editor.tag_toolbar import PronunciationDialog
from kokoro_editor.tags import find_tags
from kokoro_editor.widgets import TAG_MIME_TYPE

VOICES = ["af_bella", "af_sky", "am_michael", "am_puck"]
BOB = "#c96532"


@pytest.fixture
def editor(window):
    """The window shown, with bob (orange) and eve (a default colour)."""
    window.panel._voices = list(VOICES)
    window.panel.set_characters(
        [Character("bob", "am_puck", color=BOB), Character("eve", "af_sky")]
    )
    window.resize(900, 400)
    window.show()
    window.activateWindow()
    QTest.qWaitForWindowExposed(window)
    return window


def formats(window, block: int = 0) -> list[tuple[str, QTextCharFormat]]:
    """The highlighted pieces of a block's text, with their formats."""
    text_block = window.text_edit.document().findBlockByNumber(block)
    text = text_block.text()
    pieces = []
    for piece in text_block.layout().formats():
        start, length = piece.start, piece.length
        # Positions are UTF-16; turn them back into the text.
        units = text.encode("utf-16-le")
        chunk = units[2 * start : 2 * (start + length)].decode("utf-16-le")
        # Copied: the list Qt returns, and its formats, are freed after.
        pieces.append((chunk, QTextCharFormat(piece.format)))
    return pieces


def background(window, tag: str) -> str | None:
    for chunk, format_ in formats(window):
        if chunk == tag and format_.background().style() != Qt.NoBrush:
            return format_.background().color().name()
    return None


def underlined(window) -> list[str]:
    return [
        chunk
        for chunk, format_ in formats(window)
        if format_.underlineStyle() == QTextCharFormat.WaveUnderline
    ]


# -- highlighting ----------------------------------------------------------- #
def test_tags_are_coloured_with_or_without_spaces(editor):
    editor.text_edit.setPlainText(
        "Hi[voice:bob]there😀[voice:EVE][pause:1s][rate:1.2]"
        "[Worcester](/wˈʊstər/)[voice:am_michael(3)+af_bella(1)]"
    )
    settings = AppSettings()
    assert background(editor, "[voice:bob]") == BOB
    eve = editor.panel.colors()["eve"]
    assert background(editor, "[voice:EVE]") == eve  # names ignore case
    assert background(editor, "[pause:1s]") == DEFAULT_PAUSE_COLOR
    assert background(editor, "[rate:1.2]") == settings.rate_color
    assert background(editor, "[Worcester](/wˈʊstər/)") == (
        settings.pronunciation_color
    )
    assert background(editor, "[voice:am_michael(3)+af_bella(1)]") == (
        settings.voice_mix_color
    )
    (bob_format,) = [f for c, f in formats(editor) if c == "[voice:bob]"]
    assert bob_format.foreground().color().name() == text_color(BOB)
    assert underlined(editor) == []


def test_problems_are_underlined(editor):
    editor.text_edit.setPlainText("[voice:ghost] x [pause:1.5] [rate:9] ok")
    assert set(underlined(editor)) == {
        "[voice:ghost]",
        "[pause:1.5]",
        "[rate:9]",
    }
    # A valid tag with a problem keeps its colour under the underline.
    assert background(editor, "[rate:9]") == AppSettings().rate_color


def test_highlighting_follows_the_cast(editor):
    editor.text_edit.setPlainText("[voice:zed] hi")
    assert underlined(editor) == ["[voice:zed]"]
    editor.panel.set_characters(
        [*editor.panel.characters(), Character("zed", color="#2e8ab7")]
    )
    assert background(editor, "[voice:zed]") == "#2e8ab7"


def test_highlighting_can_be_turned_off(editor):
    editor.text_edit.setPlainText("[voice:bob] hi")
    editor.highlight_action.trigger()
    assert not load_highlight_tags()
    assert formats(editor) == []
    editor.highlight_action.trigger()
    assert background(editor, "[voice:bob]") == BOB


def test_tag_colours_in_settings(window):
    dialog = SettingsDialog(
        window.settings, window._nam, lambda _a: None, window.panel, window
    )
    dialog.tag_color_buttons["pause_color"].set_color("#123456")
    assert dialog.result_settings().pause_color == "#123456"
    settings = dialog.result_settings()
    settings.save()
    assert AppSettings.load().pause_color == "#123456"
    (reset,) = [
        b
        for b in dialog.findChildren(QPushButton)
        if b.text() == "Reset to defaults"
    ]
    reset.click()
    assert dialog.result_settings().pause_color == DEFAULT_PAUSE_COLOR
    dialog.reject()


# -- finding a tag under the mouse ------------------------------------------ #
def point_at(window, position: int) -> QPoint:
    """The middle of the character at document position."""
    edit = window.text_edit
    cursor = edit.textCursor()
    cursor.setPosition(position)
    left = edit.cursorRect(cursor)
    cursor.setPosition(position + 1)
    right = edit.cursorRect(cursor)
    return QPoint((left.x() + right.x()) // 2, left.center().y())


def test_tag_under_the_mouse(editor):
    edit = editor.text_edit
    edit.setPlainText("ab[voice:bob]cd")
    assert edit.tag_range_at(point_at(editor, 2)) == (2, 13)  # "["
    assert edit.tag_range_at(point_at(editor, 12)) == (2, 13)  # "]"
    assert edit.tag_range_at(point_at(editor, 1)) is None
    assert edit.tag_range_at(point_at(editor, 13)) is None


def test_tooltips(editor):
    tip = editor._tag_tooltip
    assert tip("[voice:BOB]", 3).startswith("bob\nVoice: am_puck")
    assert tip("[voice:af_sky+am_puck]", 3).startswith(
        "Kokoro voice mix: af_sky+am_puck"
    )
    assert "isn't a character in the current cast" in tip("[voice:zz]", 3)
    assert tip("x[pause:1.5s]", 2).startswith("A pause of 1.5 seconds.")
    assert tip("[rate:1.2]", 1).startswith("Rate 1.2×")
    assert tip("[baserate:0.8]", 1).startswith("Base rate 0.8×")
    assert tip("[Wor](/w/)", 1).startswith("“Wor” said as /w/.")
    assert tip("[pause:1.5]", 1).startswith("[pause:1.5] isn't a pause")
    assert tip("plain text", 3) is None


# -- double-click ---------------------------------------------------------- #
def double_click(window, position: int) -> None:
    QTest.mouseDClick(
        window.text_edit.viewport(),
        Qt.MouseButton.LeftButton,
        pos=point_at(window, position),
    )


def test_double_click_changes_a_voice_tags_character(editor, monkeypatch):
    edit = editor.text_edit
    edit.setPlainText("Hi[voice:bob]there [voice:bob] again")
    seen = []

    def pick_eve(dialog):
        seen.append(dialog.windowTitle())
        assert dialog.option_check.isVisibleTo(dialog)
        dialog.character_list.setCurrentRow(1)
        dialog.accept()
        return dialog.result()

    monkeypatch.setattr(CharacterPickerDialog, "exec", pick_eve)
    double_click(editor, 5)
    assert seen == ["Change Character"]
    assert edit.toPlainText() == "Hi[voice:eve]there [voice:bob] again"
    edit.undo()
    assert edit.toPlainText() == "Hi[voice:bob]there [voice:bob] again"


def test_double_click_can_change_every_tag(editor, monkeypatch):
    edit = editor.text_edit
    edit.setPlainText("[voice:bob] a [voice:BOB] b")

    def pick_eve_everywhere(dialog):
        dialog.character_list.setCurrentRow(1)
        dialog.option_check.setChecked(True)
        dialog.accept()
        return dialog.result()

    monkeypatch.setattr(CharacterPickerDialog, "exec", pick_eve_everywhere)
    double_click(editor, 2)
    assert edit.toPlainText() == "[voice:eve] a [voice:eve] b"


def test_double_click_imports_from_another_cast(editor, boxes, monkeypatch):
    save_cast("Other", [Character("zoe", "af_bella", color="#2e8ab7")])
    editor.text_edit.setPlainText("[voice:bob]x")

    def pick_zoe(dialog):
        dialog.cast_box.setCurrentText("Other")
        dialog.character_list.setCurrentRow(0)
        dialog.accept()
        return dialog.result()

    monkeypatch.setattr(CharacterPickerDialog, "exec", pick_zoe)
    boxes.answers = [QMessageBox.StandardButton.Yes]
    double_click(editor, 1)
    assert editor.text_edit.toPlainText() == "[voice:zoe]x"
    assert editor.panel.characters()[-1] == Character(
        "zoe", "af_bella", color="#2e8ab7"
    )


@pytest.mark.parametrize(
    ("text", "position", "answer", "expected"),
    [
        ("a[pause:1s]b", 3, 2.5, "a[pause:2.5s]b"),
        ("a[rate:1.2]b", 3, 0.9, "a[rate:0.9]b"),
        ("a[baserate:1.2]b", 3, 0.9, "a[baserate:0.9]b"),
    ],
)
def test_double_click_changes_pauses_and_rates(
    editor, monkeypatch, text, position, answer, expected
):
    asked = []

    def get_double(parent, title, label, current, *args):
        asked.append(current)
        return answer, True

    monkeypatch.setattr(QInputDialog, "getDouble", staticmethod(get_double))
    editor.text_edit.setPlainText(text)
    double_click(editor, position)
    assert asked == [float(find_tags(text)[0].value)]  # filled in
    assert editor.text_edit.toPlainText() == expected


def test_double_click_changes_a_pronunciation(editor, monkeypatch):
    def change_ipa(dialog):
        assert dialog.windowTitle() == "Change Pronunciation"
        assert dialog.values() == ("Worcester", "wˈʊstər")
        dialog.ipa_edit.setText("wʊstə")
        dialog.accept()
        return dialog.result()

    monkeypatch.setattr(PronunciationDialog, "exec", change_ipa)
    editor.text_edit.setPlainText("See[Worcester](/wˈʊstər/)now")
    double_click(editor, 5)
    assert editor.text_edit.toPlainText() == "See[Worcester](/wʊstə/)now"


def test_double_click_elsewhere_selects_a_word(editor):
    editor.text_edit.setPlainText("hello [voice:bob] world")
    double_click(editor, 1)
    assert editor.text_edit.textCursor().selectedText() == "hello"


# -- dragging ------------------------------------------------------------- #
def drop_tag(window, start: int, end: int, at: int, copy: bool = False):
    """Drop the tag at start..end on position at, as if dragged from the
    editor itself."""
    edit = window.text_edit
    cursor = edit.textCursor()
    cursor.setPosition(start)
    cursor.setPosition(end, cursor.MoveMode.KeepAnchor)
    tag = cursor.selectedText()
    edit._dragged = cursor
    data = QMimeData()
    data.setData(TAG_MIME_TYPE, QByteArray(tag.encode()))
    data.setText(tag)
    target = edit.cursorRect(_cursor_at(edit, at)).center()
    modifiers = (
        Qt.KeyboardModifier.ControlModifier
        if copy
        else Qt.KeyboardModifier.NoModifier
    )
    event = QDropEvent(
        QPointF(target),
        # What the editor's own drags offer.
        Qt.DropAction.MoveAction | Qt.DropAction.CopyAction,
        data,
        Qt.MouseButton.LeftButton,
        modifiers,
    )
    event.source = lambda: edit  # dragged from the editor itself
    edit.dropEvent(event)
    edit._dragged = None
    # Read now: the event refers to data freed when this returns.
    return event.dropAction()


def _cursor_at(edit, position: int):
    cursor = edit.textCursor()
    cursor.setPosition(position)
    return cursor


@pytest.mark.parametrize(
    ("text", "tag_range", "at", "expected"),
    [
        # Spaced: no double space left behind.
        (
            "one [voice:bob] two three",
            (4, 15),
            25,
            "one two three [voice:bob] ",
        ),
        # Unspaced: nothing around the tag is assumed or removed.
        ("one[voice:bob]two three", (3, 14), 23, "onetwo three [voice:bob] "),
        # Next to another tag, moved before it.
        (
            "a [pause:1s][voice:bob] b",
            (12, 23),
            2,
            "a [voice:bob] [pause:1s] b",
        ),
    ],
)
def test_dragging_moves_a_tag(editor, text, tag_range, at, expected):
    edit = editor.text_edit
    edit.setPlainText(text)
    action = drop_tag(editor, *tag_range, at)
    assert edit.toPlainText() == expected
    assert action == Qt.DropAction.CopyAction  # the drag deletes nothing
    edit.undo()  # one step
    assert edit.toPlainText() == text


def test_ctrl_drag_copies(editor):
    edit = editor.text_edit
    edit.setPlainText("[voice:bob] a b")
    drop_tag(editor, 0, 11, 15, copy=True)
    assert edit.toPlainText() == "[voice:bob] a b [voice:bob] "


def test_dropping_a_tag_on_itself_does_nothing(editor):
    edit = editor.text_edit
    edit.setPlainText("a [voice:bob] b")
    drop_tag(editor, 2, 13, 6)
    assert edit.toPlainText() == "a [voice:bob] b"


def test_dropping_inside_another_tag_lands_after_it(editor):
    edit = editor.text_edit
    edit.setPlainText("[voice:bob] x [pause:1s] y")
    drop_tag(editor, 0, 11, 18)  # inside [pause:1s]
    assert edit.toPlainText() == "x [pause:1s] [voice:bob] y"


def test_pressing_and_moving_on_a_tag_starts_a_drag(editor, monkeypatch):
    edit = editor.text_edit
    edit.setPlainText("Hi [voice:bob] there")
    dragged = []

    def fake_exec(drag, *args):
        dragged.append(bytes(drag.mimeData().data(TAG_MIME_TYPE).data()))
        return Qt.DropAction.IgnoreAction

    monkeypatch.setattr(QDrag, "exec", fake_exec)
    viewport = edit.viewport()
    start = point_at(editor, 5)
    press = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(start),
        viewport.mapToGlobal(QPointF(start)),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(viewport, press)
    far = QPointF(start + QPoint(QApplication.startDragDistance() + 5, 0))
    move = QMouseEvent(
        QMouseEvent.Type.MouseMove,
        far,
        viewport.mapToGlobal(far),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(viewport, move)
    assert dragged == [b"[voice:bob]"]
    assert edit.toPlainText() == "Hi [voice:bob] there"  # dropped nowhere


def test_a_plain_click_on_a_tag_places_the_cursor(editor):
    edit = editor.text_edit
    edit.setPlainText("Hi [voice:bob] there")
    QTest.mouseClick(
        edit.viewport(), Qt.MouseButton.LeftButton, pos=point_at(editor, 6)
    )
    assert not edit.textCursor().hasSelection()
    assert edit.textCursor().position() in (6, 7)


def test_the_picker_option_is_hidden_unless_asked_for(qapp):
    dialog = CharacterPickerDialog(None, [Character("x")])
    assert not dialog.option_check.isVisibleTo(dialog)


def test_alt_z_toggles_highlighting(editor):
    editor.text_edit.setPlainText("[voice:bob] hi")
    assert editor.highlight_action.shortcut().toString() == "Alt+Z"
    alt_z = (editor.text_edit, Qt.Key.Key_Z, Qt.KeyboardModifier.AltModifier)
    QTest.keyClick(*alt_z)
    assert formats(editor) == [] and not load_highlight_tags()
    QTest.keyClick(*alt_z)
    assert background(editor, "[voice:bob]") == BOB
