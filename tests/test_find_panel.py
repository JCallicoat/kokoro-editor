"""Find and replace: matching, stepping through matches, replacing and
the panel's place in the main window."""

from __future__ import annotations

import re

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox

from kokoro_editor.find_panel import find_matches, replacement_for
from kokoro_editor.settings import AppSettings

Btn = QMessageBox.StandardButton
CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier


# -- matching ---------------------------------------------------------------- #
def spans(text, find, regex=False):
    return [(m.start, m.end) for m in find_matches(text, find, regex)]


def test_plain_text_is_matched_literally_and_case_sensitively():
    assert spans("a.b A.B a.b", "a.b") == [(0, 3), (8, 11)]


def test_regex_matches_and_can_ignore_case():
    assert spans("cat Cat cot", "c.t", regex=True) == [(0, 3), (8, 11)]
    assert len(spans("cat Cat cot", "(?i)c.t", regex=True)) == 3


def test_nothing_to_find_matches_nothing():
    assert spans("text", "") == []


def test_empty_regex_matches_are_skipped():
    assert spans("one two", r"\b", regex=True) == []
    assert spans("one two", r"o*", regex=True) == [(0, 1), (6, 7)]


def test_invalid_regex_raises():
    with pytest.raises(re.error):
        find_matches("text", "(", True)


def test_positions_count_utf16_units():
    # The emoji takes two UTF-16 units, as QTextCursor counts them.
    assert spans("😀 hi hi", "hi") == [(3, 5), (6, 8)]


def test_replacement_expands_groups_only_in_regex_mode():
    m = re.search(r"(\w+)@(\w+)", "me@home")
    assert replacement_for(m, r"\2 at \1", True) == "home at me"
    assert replacement_for(m, r"\2 at \1", False) == r"\2 at \1"
    with pytest.raises(re.error):
        replacement_for(m, r"\3", True)


# -- in the window ----------------------------------------------------------- #
@pytest.fixture
def find(window):
    window.show()
    QTest.qWaitForWindowExposed(window)
    return window.find_panel


def set_text(window, text, cursor=0):
    window.text_edit.setPlainText(text)
    move(window, cursor)


def move(window, position):
    cursor = window.text_edit.textCursor()
    cursor.setPosition(position)
    window.text_edit.setTextCursor(cursor)


def selection(window):
    cursor = window.text_edit.textCursor()
    return cursor.selectionStart(), cursor.selectionEnd()


def highlighted(window):
    return [
        (s.cursor.selectionStart(), s.cursor.selectionEnd())
        for s in window.text_edit.extraSelections()
    ]


def test_ctrl_f_opens_the_panel_with_the_selection(window, find):
    set_text(window, "say hello there")
    cursor = window.text_edit.textCursor()
    cursor.setPosition(4)
    cursor.setPosition(9, QTextCursor.MoveMode.KeepAnchor)
    window.text_edit.setTextCursor(cursor)
    QTest.keyClick(window.text_edit, Qt.Key.Key_F, CTRL)
    assert find.isVisible()
    assert find.find_edit.text() == "hello"
    assert find.find_edit.hasFocus()
    assert highlighted(window) == [(4, 9)]


def test_selection_is_escaped_in_regex_mode(window, find):
    find.regex_check.setChecked(True)
    set_text(window, "a.b")
    window.text_edit.selectAll()
    find.open_panel()
    assert find.find_edit.text() == r"a\.b"


def test_panel_sits_in_the_editor_top_right(window, find):
    find.open_panel()
    # A scroll bar appearing narrows the text, not the editor.
    set_text(window, "line\n" * 500)
    QTest.qWait(50)
    viewport = window.text_edit.viewport().geometry()
    assert find.geometry().right() <= viewport.right()
    assert find.geometry().top() >= viewport.top()
    assert viewport.right() - find.geometry().right() < 20


def test_typing_highlights_every_match(window, find):
    set_text(window, "ab ab\nab")
    find.open_panel()
    find.find_edit.setText("ab")
    assert highlighted(window) == [(0, 2), (3, 5), (6, 8)]
    assert find.status_label.text() == "3 matches"


def test_highlights_use_the_search_colour(window, find):
    window.settings.search_color = "#123456"
    window._apply_settings()
    set_text(window, "ab")
    find.open_panel()
    find.find_edit.setText("ab")
    (selection_,) = window.text_edit.extraSelections()
    assert selection_.format.background().color() == QColor("#123456")


def test_settings_dialog_edits_the_search_colour(window):
    from kokoro_editor.settings_dialog import SettingsDialog

    dialog = SettingsDialog(
        window.settings, window._nam, lambda _: None, window.panel
    )
    dialog.search_color_button.set_color("#abcdef")
    assert dialog.result_settings().search_color == "#abcdef"


def test_search_colour_is_saved():
    AppSettings(search_color="#010203").save()
    assert AppSettings.load().search_color == "#010203"


def test_highlights_follow_edits(window, find):
    set_text(window, "ab")
    find.open_panel()
    find.find_edit.setText("ab")
    window.text_edit.appendPlainText("ab")
    QTest.qWait(300)
    assert highlighted(window) == [(0, 2), (3, 5)]


def test_find_starts_at_the_cursor_and_wraps(window, find):
    set_text(window, "ab ab ab", cursor=2)
    find.open_panel()
    find.find_edit.setText("ab")
    QTest.keyClick(window, Qt.Key.Key_G, CTRL)
    assert selection(window) == (3, 5)
    assert find.status_label.text() == "2 of 3"
    find.next_btn.click()
    assert selection(window) == (6, 8)
    find.next_btn.click()
    assert selection(window) == (0, 2)
    assert "wrapped to the top" in find.status_label.text()
    find.previous_btn.click()
    assert selection(window) == (6, 8)
    assert "wrapped to the bottom" in find.status_label.text()
    QTest.keyClick(window, Qt.Key.Key_G, CTRL | SHIFT)
    assert selection(window) == (3, 5)


def test_find_scrolls_the_match_into_view(window, find):
    set_text(window, "line\n" * 500 + "needle")
    find.open_panel()
    find.find_edit.setText("needle")
    find.action_btn.click()
    edit = window.text_edit
    assert edit.verticalScrollBar().value() > 0
    assert edit.viewport().rect().contains(edit.cursorRect())


def test_enter_in_find_steps_through_matches(window, find):
    set_text(window, "ab ab")
    find.open_panel()
    find.find_edit.setText("ab")
    QTest.keyClick(find.find_edit, Qt.Key.Key_Return)
    assert selection(window) == (0, 2)
    QTest.keyClick(find.find_edit, Qt.Key.Key_Return)
    assert selection(window) == (3, 5)
    QTest.keyClick(find.find_edit, Qt.Key.Key_Return, SHIFT)
    assert selection(window) == (0, 2)


def test_invalid_regex_is_shown_in_the_panel(window, find, boxes):
    find.open_panel()
    find.regex_check.setChecked(True)
    find.find_edit.setText("(")
    find.action_btn.click()
    assert find.status_label.text().startswith("Invalid regex")
    assert boxes.messages == []


def test_action_button_follows_the_replacement(find):
    assert find.action_btn.text() == "Search"
    find.replace_edit.setText("x")
    assert find.action_btn.text() == "Replace"
    find.all_check.setChecked(True)
    assert find.action_btn.text() == "Replace"


def test_tab_cycles_within_the_panel(window, find):
    set_text(window, "text")
    find.open_panel()
    QTest.keyClick(find.find_edit, Qt.Key.Key_Tab)
    assert find.regex_check.hasFocus()
    QTest.keyClick(find.regex_check, Qt.Key.Key_Tab)
    assert find.replace_edit.hasFocus()
    find.close_btn.setFocus()
    QTest.keyClick(find.close_btn, Qt.Key.Key_Tab)
    assert find.find_edit.hasFocus()
    QTest.keyClick(find.find_edit, Qt.Key.Key_Backtab, SHIFT)
    assert find.close_btn.hasFocus()
    assert window.text_edit.toPlainText() == "text"


def test_replace_asks_about_each_match(window, find, boxes):
    set_text(window, "ab ab ab")
    find.open_panel()
    find.find_edit.setText("ab")
    find.replace_edit.setText("xyz")
    boxes.answers = [Btn.Yes, Btn.No, Btn.Cancel]
    find.action_btn.click()  # Yes
    assert window.text_edit.toPlainText() == "xyz ab ab"
    assert selection(window) == (4, 6)  # on to the next
    QTest.keyClick(window, Qt.Key.Key_G, CTRL)  # No
    assert window.text_edit.toPlainText() == "xyz ab ab"
    assert selection(window) == (7, 9)
    find.action_btn.click()  # Cancel
    assert window.text_edit.toPlainText() == "xyz ab ab"
    assert selection(window) == (7, 9)
    assert len(boxes.messages) == 3
    assert "“xyz”" in boxes.last


def test_replace_expands_regex_groups(window, find, boxes):
    set_text(window, "me@home")
    find.open_panel()
    find.regex_check.setChecked(True)
    find.find_edit.setText(r"(\w+)@(\w+)")
    find.replace_edit.setText(r"\2 at \1")
    find.action_btn.click()
    assert window.text_edit.toPlainText() == "home at me"


def test_bad_group_reference_is_shown_not_replaced(window, find, boxes):
    set_text(window, "ab")
    find.open_panel()
    find.regex_check.setChecked(True)
    find.find_edit.setText("ab")
    find.replace_edit.setText(r"\1")
    find.action_btn.click()
    assert window.text_edit.toPlainText() == "ab"
    assert find.status_label.text().startswith("Bad replacement")
    assert boxes.messages == []


def test_replace_all_is_one_undo_step(window, find, boxes):
    set_text(window, "ab ab\nab")
    find.open_panel()
    find.find_edit.setText("ab")
    find.replace_edit.setText("😀")
    find.all_check.setChecked(True)
    find.action_btn.click()
    assert "Replace 3 matches" in boxes.last
    assert window.text_edit.toPlainText() == "😀 😀\n😀"
    assert highlighted(window) == []
    window.text_edit.undo()
    assert window.text_edit.toPlainText() == "ab ab\nab"


def test_replace_all_can_be_declined(window, find, boxes):
    set_text(window, "ab ab")
    find.open_panel()
    find.find_edit.setText("ab")
    find.replace_edit.setText("x")
    find.all_check.setChecked(True)
    boxes.answers = [Btn.No]
    find.action_btn.click()
    assert window.text_edit.toPlainText() == "ab ab"


def test_escape_closes_and_clears_highlights(window, find):
    set_text(window, "ab")
    find.open_panel()
    find.find_edit.setText("ab")
    QTest.keyClick(find.find_edit, Qt.Key.Key_Escape)
    assert not find.isVisible()
    assert highlighted(window) == []
    assert window.text_edit.hasFocus()


def test_escape_in_the_editor_closes_the_panel(window, find):
    find.open_panel()
    window.text_edit.setFocus()
    QTest.keyClick(window.text_edit, Qt.Key.Key_Escape)
    assert not find.isVisible()


def test_ctrl_g_reopens_a_closed_panel(window, find):
    set_text(window, "ab ab", cursor=1)
    find.open_panel()
    find.find_edit.setText("ab")
    find.close_btn.click()
    QTest.keyClick(window, Qt.Key.Key_G, CTRL)
    assert find.isVisible()
    assert selection(window) == (3, 5)
