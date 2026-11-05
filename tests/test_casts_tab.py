"""The Casts settings tab: renaming, import, export and inserting."""

from __future__ import annotations

import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox

from kokoro_editor import casts_tab
from kokoro_editor.casts_tab import (
    CastsTab,
    ConflictDialog,
    InsertCharactersDialog,
)
from kokoro_editor.characters import Character, cast_to_json
from kokoro_editor.characters_panel import PanelWidget
from kokoro_editor.settings import (
    AppSettings,
    load_casts,
    load_last_cast,
    save_cast,
    save_casts,
)

Btn = QMessageBox.StandardButton


@pytest.fixture
def panel(qapp, boxes, saga):
    save_cast("Alpha", [Character("a")])
    save_cast("Zed", [Character("z")])
    panel = PanelWidget(AppSettings(), None, lambda _active: None)
    panel.load_cast("Saga")
    return panel


@pytest.fixture
def tab(panel):
    tab = CastsTab(panel)
    tab.show()
    QTest.qWaitForWindowExposed(tab)
    return tab


def listed(tab) -> list[str]:
    return [tab.cast_list.item(i).text() for i in range(tab.cast_list.count())]


def conflicts(monkeypatch, *answers):
    """Answer each conflict dialog with a function of the dialog, which
    returns the dialog's result. Returns the dialogs' suggested names."""
    queue, suggested = list(answers), []

    def fake_exec(dialog):
        suggested.append(dialog.rename_edit.text())
        return queue.pop(0)(dialog)

    monkeypatch.setattr(ConflictDialog, "exec", fake_exec)
    return suggested


def choose_files(monkeypatch, path):
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *a, **k: (path, "")),
    )
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *a, **k: (path, "")),
    )


def test_lists_casts_with_the_current_one_selected(tab):
    assert listed(tab) == ["Alpha", "Saga", "Zed"]
    assert tab.cast_list.currentItem().text() == "Saga"
    assert tab.insert_btn.isEnabled()


def rename_in_place(tab, row: int, new: str, double_click: bool) -> None:
    item = tab.cast_list.item(row)
    if double_click:
        viewport = tab.cast_list.viewport()
        pos = tab.cast_list.visualItemRect(item).center()
        # Item views only edit on a double-click after a press on the item.
        QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=pos)
        QTest.mouseDClick(viewport, Qt.MouseButton.LeftButton, pos=pos)
    else:
        tab.cast_list.setCurrentRow(row)
        tab.rename_btn.click()
    editor = QApplication.focusWidget()
    editor.selectAll()
    QTest.keyClicks(editor, new)
    QTest.keyClick(editor, Qt.Key.Key_Return)
    QTest.qWait(20)


def test_rename_by_double_click(tab, panel):
    rename_in_place(tab, 1, "Tale", double_click=True)
    # Renamed in place, keeping the stored order.
    assert list(load_casts()) == ["Tale", "Alpha", "Zed"]
    assert panel.cast_name() == "Tale" and load_last_cast() == "Tale"
    assert panel.cast_box.currentText() == "Tale"


def test_rename_refuses_a_taken_name(tab, boxes):
    rename_in_place(tab, 1, "alpha", double_click=False)
    assert boxes.last == "There is already a cast named alpha."
    assert tab.cast_list.item(1).text() == "Saga"
    assert "Saga" in load_casts()


def test_export(tab, panel, boxes, saga, tmp_path, monkeypatch):
    path = tmp_path / "saga.json"
    choose_files(monkeypatch, str(path))
    panel.set_characters(saga + [Character("unsaved")])
    boxes.answers = [Btn.Discard]
    tab.export_btn.click()
    assert boxes.last == "Save changes to the cast Saga?"
    assert json.loads(path.read_text()) == cast_to_json(saga)


def write_cast_file(tmp_path, aliases) -> str:
    path = tmp_path / "in.json"
    path.write_text(json.dumps({"voice_aliases": aliases}))
    return str(path)


def overwrite(dialog):
    dialog.overwrite_radio.setChecked(True)
    return QDialog.DialogCode.Accepted


def test_import_with_conflicts(tab, boxes, tmp_path, monkeypatch):
    choose_files(
        monkeypatch,
        write_cast_file(tmp_path, {"a": "af_new", "b": "x", "x y": "bad"}),
    )
    conflicts(monkeypatch, overwrite)
    tab.cast_list.setCurrentRow(0)  # Alpha
    tab.import_btn.click()
    assert load_casts()["Alpha"] == [
        Character("a", "af_new"),
        Character("b", "x"),
    ]
    assert boxes.last == (
        "Alpha: 1 added, 1 replaced.\n\n"
        "Not imported (unusable name or rate): x y"
    )


def test_import_rename_and_cancel(tab, boxes, tmp_path, monkeypatch):
    choose_files(
        monkeypatch, write_cast_file(tmp_path, {"bob": "x", "alice": "y"})
    )

    def rename(dialog):
        dialog.rename_check.setChecked(True)
        assert not dialog.keep_radio.isEnabled()
        dialog.rename_edit.setText("narrator")  # taken: refused
        dialog.accept()
        assert boxes.last == "There is already a character named narrator."
        dialog.rename_edit.setText("bobby")
        dialog.accept()
        return dialog.result()

    suggested = conflicts(
        monkeypatch, rename, lambda _dialog: QDialog.DialogCode.Rejected
    )
    tab.import_btn.click()  # into Saga, the current cast
    assert suggested == ["bob_2", "alice_2"]
    assert [c.name for c in load_casts()["Saga"]] == [
        "narrator",
        "bob",
        "alice",
        "bobby",
    ]
    assert boxes.last.startswith("Saga: 0 added, 1 renamed, 1 skipped.")


def test_apply_to_all_remaining_conflicts(tab, boxes, tmp_path, monkeypatch):
    choose_files(
        monkeypatch,
        write_cast_file(
            tmp_path, {"narrator": "x", "bob": "y", "alice": "z", "new": "w"}
        ),
    )

    def rename_all(dialog):
        dialog.rename_check.setChecked(True)
        dialog.apply_all_check.setChecked(True)
        return QDialog.DialogCode.Accepted

    suggested = conflicts(monkeypatch, rename_all)
    tab.import_btn.click()
    assert len(suggested) == 1  # asked once, then applied to the rest
    assert [c.name for c in load_casts()["Saga"]] == [
        "narrator",
        "bob",
        "alice",
        "narrator_2",
        "bob_2",
        "alice_2",
        "new",
    ]


def test_import_reports_a_file_that_is_not_a_cast(
    tab, boxes, tmp_path, monkeypatch
):
    path = tmp_path / "bad.json"
    path.write_text("[1, 2]")
    choose_files(monkeypatch, str(path))
    tab.import_btn.click()
    assert boxes.messages[-1][0] == "critical"
    assert "Expected a JSON object of voice aliases." in boxes.last


def test_import_with_no_casts_makes_one(
    qapp, boxes, saga, tmp_path, monkeypatch
):
    save_casts({})
    panel = PanelWidget(AppSettings(), None, lambda _active: None)
    tab = CastsTab(panel)
    assert not tab.export_btn.isEnabled() and not tab.insert_btn.isEnabled()
    path = tmp_path / "heroes.json"
    path.write_text(json.dumps(cast_to_json(saga)))
    choose_files(monkeypatch, str(path))
    tab.import_btn.click()
    assert load_casts() == {"heroes": saga}
    assert tab._selected() == "heroes"


# -- insert --------------------------------------------------------------- #
def sources(dialog) -> list[str]:
    box = dialog.source_box
    return [box.itemText(i) for i in range(box.count())]


def test_insert_dialog_sources_leave_out_the_target(panel, saga):
    dialog = InsertCharactersDialog(load_casts(), "Saga")
    assert dialog.target_box.currentText() == "Saga"
    assert sources(dialog) == ["Alpha", "Zed"]
    assert not dialog.insert_btn.isEnabled()
    dialog.target_box.setCurrentText("Alpha")
    assert sources(dialog) == ["Saga", "Zed"]
    assert dialog.source_box.currentText() == "Saga"
    dialog.target_box.setCurrentText("Zed")
    assert dialog.source_box.currentText() == "Saga"  # kept while offered
    dialog.character_list.item(2).setSelected(True)
    dialog.character_list.item(0).setSelected(True)
    assert dialog.insert_btn.isEnabled()
    assert dialog.selection() == ([saga[0], saga[2]], "Zed")


def test_insert_into_the_current_cast(tab, panel, saga, monkeypatch):
    def pick_z(dialog):
        dialog.target_box.setCurrentText("Saga")
        dialog.source_box.setCurrentText("Zed")
        dialog.character_list.item(0).setSelected(True)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(InsertCharactersDialog, "exec", pick_z)
    tab.insert_btn.click()
    assert load_casts()["Saga"] == [*saga, Character("z")]
    assert panel.characters() == [*saga, Character("z")]
    assert not panel.cast_modified()


def test_free_names():
    assert casts_tab._free_character_name("bob", ["bob", "Bob_2"]) == "bob_3"
    assert len(casts_tab._free_character_name("x" * 24, ["x" * 24])) == 24
    assert casts_tab._free_cast_name("Saga", ["saga", "Saga (2)"]) == (
        "Saga (3)"
    )
