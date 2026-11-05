"""The characters panel: characters, casts, reordering and the dialogs it
opens."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QCloseEvent, QDropEvent
from PySide6.QtWidgets import QDialog, QMessageBox

from kokoro_editor import characters_panel
from kokoro_editor.characters import CHARACTER_MIME_TYPE, Character
from kokoro_editor.characters_panel import (
    CharacterDialog,
    CharacterPickerDialog,
    PanelWidget,
)
from kokoro_editor.settings import (
    AppSettings,
    load_casts,
    load_last_cast,
    load_last_characters,
    save_cast,
)

Btn = QMessageBox.StandardButton


@pytest.fixture
def panel(qapp, boxes):
    return PanelWidget(AppSettings(), None, lambda _active: None)


def names(panel) -> list[str]:
    return [c.name for c in panel.characters()]


def edit_in_dialog(monkeypatch, *results: Character | None):
    """Make the character dialog enter each result in turn and press OK
    (or Cancel for None). Returns the dialogs opened."""
    queue, opened = list(results), []

    def fake_exec(dialog):
        opened.append(
            (dialog.windowTitle(), dialog.character(), dialog._other_names)
        )
        result = queue.pop(0)
        if result is None:
            return QDialog.DialogCode.Rejected
        dialog.name_edit.setText(result.name)
        dialog.voice_mix.set_mix(
            *characters_panel.parse_voice_string(result.voice)
        )
        dialog.rate_spin.setValue(result.rate)
        dialog.accept()
        return dialog.result()

    monkeypatch.setattr(CharacterDialog, "exec", fake_exec)
    return opened


# -- characters --------------------------------------------------------- #
def test_add_edit_and_cancel(panel, monkeypatch):
    opened = edit_in_dialog(
        monkeypatch,
        Character("bob", "am_puck"),
        Character("rob", "am_puck", 0.8),
        None,
    )
    renames = []
    panel.character_renamed.connect(
        lambda old, new: renames.append((old, new))
    )
    panel._on_add()
    panel._edit_row(0)
    panel._edit_row(0)
    assert [title for title, _c, _o in opened] == [
        "Add Character",
        "Edit Character",
        "Edit Character",
    ]
    assert opened[1][1] == Character("bob", "am_puck")  # filled in for editing
    assert panel.characters() == [Character("rob", "am_puck", 0.8)]
    assert renames == [("bob", "rob")]
    assert panel.table.item(0, 0).toolTip() == (
        "rob\nVoice: am_puck\nRate: 0.80×"
    )


def test_character_dialog_refuses_bad_names(qapp, boxes):
    dialog = CharacterDialog(
        [],
        AppSettings,
        None,
        lambda _a: None,
        Character("x"),
        ["Bob"],
    )
    for name in ("", "-x", "bob"):
        dialog.name_edit.setText(name)
        dialog.accept()
        assert dialog.result() != QDialog.DialogCode.Accepted
    assert boxes.last == "There is already a character named bob."
    dialog.name_edit.setText("  ok  ")
    dialog.rate_spin.setValue(0.85000001)
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.character() == Character("ok", "", 0.85)


def test_rate_tooltip_gives_detail_with_shift(qapp):
    settings = AppSettings(speed=1.1)
    dialog = CharacterDialog(
        [],
        lambda: settings,
        None,
        lambda _a: None,
        Character("x", "", 0.8),
        [],
    )
    assert "Hold Shift" in dialog.rate_spin._tooltip(False)
    assert "about 0.88×" in dialog.rate_spin._tooltip(True)


def test_remove(panel, boxes):
    panel.set_characters([Character("bob"), Character("eve")])
    panel.set_tag_counter(lambda name: 2 if name == "bob" else 0)
    panel.table.selectRow(0)
    boxes.answers = [Btn.No]
    panel._on_remove()
    assert boxes.last.startswith("bob is tagged 2 times in the document.")
    panel.table.selectRow(1)
    boxes.answers = [Btn.Yes]
    panel._on_remove()
    assert boxes.last == "Remove eve?"
    assert names(panel) == ["bob"]


# -- reordering --------------------------------------------------------- #
def test_move_buttons(panel):
    panel.set_characters([Character(n) for n in "abc"])
    assert not panel.up_btn.isEnabled() and not panel.down_btn.isEnabled()
    panel.table.selectRow(0)
    assert not panel.up_btn.isEnabled() and panel.down_btn.isEnabled()
    panel.down_btn.click()
    assert names(panel) == ["b", "a", "c"]
    assert panel.table.currentRow() == 1
    panel.table.selectRow(2)
    assert panel.up_btn.isEnabled() and not panel.down_btn.isEnabled()


@pytest.mark.parametrize(
    ("source", "target", "order", "row"),
    [
        (0, 2, "bacd", 1),  # above the target row
        (3, 1, "adbc", 1),
        (1, 4, "acdb", 3),  # below the last row: to the end
        (2, 2, "abcd", 2),  # onto itself
        (1, 2, "abcd", 1),  # onto the next row: already above it
    ],
)
def test_drop_rules(panel, source, target, order, row):
    panel.set_characters([Character(n) for n in "abcd"])
    panel.table.selectRow(source)
    panel._on_row_dropped(source, target)
    assert "".join(names(panel)) == order
    assert panel.table.currentRow() == row


def test_drop_event_reorders_only_own_rows(panel, monkeypatch):
    panel.set_characters([Character(n) for n in "abcd"])
    panel.set_collapsed(False)
    panel.show()
    table = panel.table
    table.selectRow(0)
    data = table.mimeData([table.item(0, 0)])
    target = QPointF(QPoint(10, table.rowViewportPosition(2) + 2))

    def drop():
        event = QDropEvent(
            target,
            Qt.DropAction.CopyAction,
            data,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        table.dropEvent(event)
        return event

    assert not drop().isAccepted()  # no drag source: from elsewhere
    monkeypatch.setattr(QDropEvent, "source", lambda _event: table)
    event = drop()
    assert event.isAccepted()
    assert event.dropAction() == Qt.DropAction.CopyAction
    assert "".join(names(panel)) == "bacd"
    assert [table.item(r, 0).text() for r in range(4)] == list("bacd")


def test_drag_data(panel):
    panel.set_characters([Character("bob")])
    data = panel.table.mimeData([panel.table.item(0, 0)])
    assert bytes(data.data(CHARACTER_MIME_TYPE).data()) == b"bob"
    assert data.text() == "[voice:bob]"


def test_collapse(panel):
    seen = []
    panel.collapsed_changed.connect(seen.append)
    tip = panel.toggle_btn.toolTip
    assert panel.is_collapsed() and tip().startswith("Show")
    panel.toggle_btn.click()
    assert not panel.is_collapsed() and tip().startswith("Hide")
    assert panel.width() == PanelWidget.EXPANDED_WIDTH
    panel.set_collapsed(False)  # no change, no signal
    assert seen == [False]


# -- casts ---------------------------------------------------------------- #
def cast_state(panel):
    box = panel.cast_box
    return (
        box.currentText(),
        [box.itemText(i) for i in range(box.count())],
        panel.save_cast_btn.isEnabled(),
        panel.delete_cast_btn.isEnabled(),
    )


def test_saving_an_unnamed_cast_asks_for_a_name(qapp, boxes):
    save_cast("Other", [])
    panel = PanelWidget(AppSettings(), None, lambda _active: None)
    panel.set_characters([Character("bob")])
    assert cast_state(panel) == ("", ["Other"], True, False)
    boxes.texts = [("", True), (" other ", True), ("Story", True)]
    assert panel.save_cast()
    assert boxes.texts_shown == [
        "Enter a name for the cast.",
        "There is already a cast named other.",
    ]
    assert cast_state(panel) == ("Story", ["Other", "Story"], False, True)
    assert load_casts()["Story"] == [Character("bob")]
    assert load_last_cast() == "Story"


def test_cast_is_unsaved_only_while_it_differs(panel, saga):
    panel.load_cast("Saga")
    panel.set_characters(saga + [Character("x")])
    assert panel.cast_modified()
    panel.set_characters(saga)
    assert not panel.cast_modified()


@pytest.mark.parametrize(
    ("answer", "switched", "saved_voice"),
    [
        (Btn.Cancel, False, "af_bella"),
        (Btn.Discard, True, "af_bella"),
        (Btn.Save, True, "am_michael"),
    ],
)
def test_switching_with_unsaved_changes(
    panel, boxes, saga, answer, switched, saved_voice
):
    save_cast("Other", [Character("eve")])
    panel.load_cast("Saga")
    changed = [*saga[:2], Character("alice", "am_michael")]
    panel.set_characters(changed)
    boxes.answers = [answer]
    panel._on_cast_activated(panel.cast_box.findText("Other"))
    assert boxes.last == "Save changes to the cast Saga?"
    assert panel.cast_name() == ("Other" if switched else "Saga")
    assert panel.cast_box.currentText() == panel.cast_name()
    assert load_casts()["Saga"][2].voice == saved_voice


def test_new_cast_starts_empty_and_saved(panel, boxes, saga):
    panel.load_cast("Saga")
    boxes.texts = [("Second", True)]
    panel._on_new_cast()
    assert cast_state(panel)[:3] == ("Second", ["Saga", "Second"], False)
    assert load_casts()["Second"] == [] and panel.characters() == []
    boxes.texts = [("", False)]  # cancelled
    panel._on_new_cast()
    assert panel.cast_name() == "Second"


def test_delete_cast(panel, boxes, saga):
    panel.load_cast("Saga")
    boxes.answers = [Btn.No]
    panel._on_delete_cast()
    assert "Saga" in load_casts()
    boxes.answers = [Btn.Yes]
    panel._on_delete_cast()
    assert cast_state(panel) == ("", [], False, False)
    assert load_casts() == {} and load_last_cast() == ""


def test_changes_from_the_casts_tab(panel, saga):
    panel.load_cast("Saga")
    panel.cast_renamed("Saga", "Tale")
    assert panel.cast_name() == "Tale" and load_last_cast() == "Tale"
    save_cast("Tale", [Character("new")])
    panel.casts_changed("Tale")
    assert panel.characters() == [Character("new")]


# -- character picker (Alt+0 and tag replacement) ----------------------- #
def picker_rows(dialog) -> list[str]:
    lst = dialog.character_list
    return [lst.item(i).text() for i in range(lst.count())]


def test_picker_opens_on_the_current_cast(qapp, saga):
    save_cast("Other", [Character("eve")])
    dialog = CharacterPickerDialog("Saga", saga + [Character("unsaved")])
    assert dialog.cast_box.currentText() == "Saga"
    assert picker_rows(dialog) == ["narrator", "bob", "alice", "unsaved"]
    dialog.cast_box.setCurrentText("Other")
    assert picker_rows(dialog) == ["eve"]


def test_picker_remembers_the_last_pick_per_cast(qapp, saga):
    dialog = CharacterPickerDialog("Saga", saga)
    dialog.character_list.setCurrentRow(2)
    dialog.accept()
    assert load_last_characters() == {"Saga": "alice"}
    again = CharacterPickerDialog("Saga", saga)
    assert again.character_list.currentItem().text() == "alice"
    assert again.choice() == ("Saga", "alice")


def test_picker_with_an_unsaved_or_empty_cast(qapp):
    save_cast("Empty", [])
    unsaved = CharacterPickerDialog(None, [Character("tmp")])
    assert unsaved.cast_box.itemText(0) == "(unsaved cast)"
    assert unsaved.choice() == (None, "tmp")
    empty = CharacterPickerDialog("Empty", [])
    ok = empty.buttons.button(empty.buttons.StandardButton.Ok)
    assert not ok.isEnabled() and empty.choice() is None


def test_picker_accept_check_can_keep_it_open(qapp, saga):
    allowed = []
    dialog = CharacterPickerDialog(
        "Saga",
        saga,
        title="Replace Voice Tag",
        message="Replace it with:",
        ok_text="Replace",
        cancel_text="Skip",
        accept_check=lambda choice: allowed.pop(0),
    )
    buttons = dialog.buttons
    assert buttons.button(buttons.StandardButton.Ok).text() == "Replace"
    assert buttons.button(buttons.StandardButton.Cancel).text() == "Skip"
    allowed[:] = [False, True]
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_closing_the_window_asks_about_an_unsaved_cast(window, boxes, saga):
    window.panel.load_cast("Saga")
    window.panel.set_characters([Character("x")])
    boxes.answers = [Btn.Cancel]
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted()
    assert boxes.last == "Save changes to the cast Saga?"
