"""Casts tab of the Settings dialog: rename saved casts, import and export
them as cast files, and copy characters between them.

Everything here is saved straight away rather than when Settings is
accepted, like the panel's own cast buttons.
"""

from __future__ import annotations

import json
import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from kokoro_editor.characters import (
    Character,
    MergeReport,
    cast_name_error,
    cast_to_json,
    characters_from_json,
    merge_characters,
    name_error,
)
from kokoro_editor.characters_panel import PanelWidget
from kokoro_editor.settings import (
    load_casts,
    load_last_dir,
    rename_cast,
    save_cast,
    save_last_dir,
)

JSON_FILTER = "JSON files (*.json);;All files (*)"


def _free_character_name(name: str, taken: list[str]) -> str:
    """name with the lowest _2, _3… suffix that isn't taken, kept within
    the 24 characters a name allows."""
    folded = {n.casefold() for n in taken}
    number = 2
    while True:
        suffix = f"_{number}"
        candidate = name[: 24 - len(suffix)] + suffix
        if candidate.casefold() not in folded:
            return candidate
        number += 1


def _free_cast_name(name: str, taken: list[str]) -> str:
    candidate, number = name, 2
    while cast_name_error(candidate, taken):
        candidate = f"{name} ({number})"
        number += 1
    return candidate


def _summary(cast: str, report: MergeReport, unusable: list[str]) -> str:
    counts = [f"{len(report.added)} added"]
    for label, names in (
        ("replaced", report.replaced),
        ("renamed", report.renamed),
        ("skipped", report.skipped),
    ):
        if names:
            counts.append(f"{len(names)} {label}")
    text = f"{cast}: {', '.join(counts)}."
    if unusable:
        text += "\n\nNot imported (unusable name or rate): " + ", ".join(
            unusable
        )
    return text


class ConflictDialog(QDialog):
    """Asks what to do with an incoming character whose name the target
    cast already has: keep the existing one, overwrite it, or add the
    incoming one under a new name. Cancel skips it."""

    def __init__(
        self,
        character: Character,
        cast: str,
        taken_names: list[str],
        allow_apply_all: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Character Already Exists")
        self._taken = taken_names

        message = QLabel(
            f"The cast {cast} already has a character named {character.name}."
        )
        message.setWordWrap(True)
        self.keep_radio = QRadioButton("Keep the existing character")
        self.overwrite_radio = QRadioButton(
            "Overwrite it with the incoming one"
        )
        self.keep_radio.setChecked(True)
        self.rename_check = QCheckBox("Rename the incoming character to:")
        self.rename_edit = QLineEdit(
            _free_character_name(character.name, taken_names)
        )
        self.rename_edit.setEnabled(False)
        self.rename_check.toggled.connect(self._on_rename_toggled)
        self.apply_all_check = QCheckBox(
            "Do the same for the remaining conflicts"
        )
        self.apply_all_check.setToolTip(
            "Renamed characters get the next free numbered name"
        )
        self.apply_all_check.setVisible(allow_apply_all)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(message)
        layout.addWidget(self.keep_radio)
        layout.addWidget(self.overwrite_radio)
        layout.addWidget(self.rename_check)
        layout.addWidget(self.rename_edit)
        layout.addWidget(self.apply_all_check)
        layout.addWidget(buttons)

    def _on_rename_toggled(self, checked: bool) -> None:
        # Renaming adds the incoming character, so keep/overwrite don't apply.
        self.rename_edit.setEnabled(checked)
        self.keep_radio.setEnabled(not checked)
        self.overwrite_radio.setEnabled(not checked)
        if checked:
            self.rename_edit.setFocus()
            self.rename_edit.selectAll()

    def resolution(self) -> tuple[str, str]:
        """An action for merge_characters: ("skip" | "overwrite" |
        "rename", new name)."""
        if self.rename_check.isChecked():
            return "rename", self.rename_edit.text().strip()
        if self.overwrite_radio.isChecked():
            return "overwrite", ""
        return "skip", ""

    def accept(self) -> None:
        action, name = self.resolution()
        error = name_error(name, self._taken) if action == "rename" else None
        if error:
            QMessageBox.warning(self, "Invalid name", error)
            self.rename_edit.setFocus()
            return
        super().accept()


class ConflictResolver:
    """A resolve function for merge_characters that asks with
    ConflictDialog, then answers by itself once a choice is made for all
    remaining conflicts. Cancel skips just that character."""

    def __init__(
        self,
        cast: str,
        parent: QWidget | None = None,
        allow_apply_all: bool = True,
    ) -> None:
        self._cast = cast
        self._parent = parent
        self._allow_apply_all = allow_apply_all
        self._for_all: str | None = None
        self.cancelled = False  # whether the last conflict was cancelled

    def __call__(
        self, character: Character, taken: list[str]
    ) -> tuple[str, str]:
        if self._for_all == "rename":
            return "rename", _free_character_name(character.name, taken)
        if self._for_all is not None:
            return self._for_all, ""
        dialog = ConflictDialog(
            character, self._cast, taken, self._allow_apply_all, self._parent
        )
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        resolution = dialog.resolution()
        apply_all = dialog.apply_all_check.isChecked()
        dialog.deleteLater()
        self.cancelled = not accepted
        if not accepted:
            return "skip", ""
        if apply_all:
            self._for_all = resolution[0]
        return resolution


class InsertCharactersDialog(QDialog):
    """Pick characters from one saved cast to add to another."""

    def __init__(
        self,
        casts: dict[str, list[Character]],
        target: str | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Insert Characters")
        self.setMinimumWidth(300)
        self._casts = casts

        self.source_box = QComboBox()
        self.character_list = QListWidget()
        self.character_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.target_box = QComboBox()
        self.target_box.addItems(list(casts))
        self.target_box.setCurrentIndex(
            max(self.target_box.findText(target or ""), 0)
        )

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.insert_btn = buttons.addButton(
            "Insert", QDialogButtonBox.ButtonRole.AcceptRole
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        source_form = QFormLayout()
        source_form.addRow("From cast:", self.source_box)
        target_form = QFormLayout()
        target_form.addRow("Into cast:", self.target_box)
        layout = QVBoxLayout(self)
        layout.addLayout(source_form)
        layout.addWidget(self.character_list, 1)
        layout.addLayout(target_form)
        layout.addWidget(buttons)

        self.target_box.currentIndexChanged.connect(self._fill_sources)
        self.source_box.currentIndexChanged.connect(self._fill_characters)
        self.character_list.itemSelectionChanged.connect(
            self._update_insert_button
        )
        self._fill_sources()

    def _fill_sources(self) -> None:
        """Every cast but the target, keeping the source shown if it's
        still offered, else the first saved cast."""
        target = self.target_box.currentText()
        shown = self.source_box.currentText()
        sources = [name for name in self._casts if name != target]
        self.source_box.blockSignals(True)
        self.source_box.clear()
        self.source_box.addItems(sources)
        self.source_box.setCurrentIndex(
            sources.index(shown) if shown in sources else 0
        )
        self.source_box.blockSignals(False)
        self._fill_characters()

    def _fill_characters(self) -> None:
        self.character_list.clear()
        for character in self._casts.get(self.source_box.currentText(), []):
            item = QListWidgetItem(character.name)
            item.setToolTip(character.tooltip())
            self.character_list.addItem(item)
        self._update_insert_button()

    def _update_insert_button(self) -> None:
        self.insert_btn.setEnabled(bool(self.character_list.selectedItems()))

    def selection(self) -> tuple[list[Character], str]:
        """The selected characters, in the source cast's order, and the
        target cast."""
        chosen = {item.text() for item in self.character_list.selectedItems()}
        source = self._casts.get(self.source_box.currentText(), [])
        return [
            c for c in source if c.name in chosen
        ], self.target_box.currentText()


class CastsTab(QWidget):
    def __init__(
        self, panel: PanelWidget, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._panel = panel

        self.cast_list = QListWidget()
        # Double-click or F2 renames, like the Rename button.
        self.cast_list.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.cast_list.itemChanged.connect(self._on_item_changed)
        self.cast_list.currentItemChanged.connect(self._update_buttons)

        self.rename_btn = QPushButton("Rename")
        self.rename_btn.clicked.connect(self._rename)
        rename_row = QHBoxLayout()
        rename_row.addWidget(self.rename_btn)
        rename_row.addStretch(1)

        self.import_btn = QPushButton("Import…")
        self.import_btn.setToolTip(
            "Add the characters in a cast file to the selected cast"
        )
        self.import_btn.clicked.connect(self._import)
        self.export_btn = QPushButton("Export…")
        self.export_btn.setToolTip("Save the selected cast as a cast file")
        self.export_btn.clicked.connect(self._export)
        self.insert_btn = QPushButton("Insert…")
        self.insert_btn.setToolTip("Copy characters from one cast to another")
        self.insert_btn.clicked.connect(self._insert)
        file_row = QHBoxLayout()
        file_row.addWidget(self.import_btn)
        file_row.addWidget(self.export_btn)
        file_row.addWidget(self.insert_btn)
        file_row.addStretch(1)

        note = QLabel(
            "Changes on this tab are saved straight away. Cast files use "
            "the voice_aliases format of Kokoro-FastAPI's web UI."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")

        layout = QVBoxLayout(self)
        layout.addWidget(self.cast_list, 1)
        layout.addLayout(rename_row)
        layout.addLayout(file_row)
        layout.addWidget(note)

        self._refresh(panel.cast_name())

    # -- list --------------------------------------------------------------- #
    def _refresh(self, select: str | None) -> None:
        names = sorted(load_casts(), key=str.casefold)
        self.cast_list.blockSignals(True)
        self.cast_list.clear()
        for name in names:
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.cast_list.addItem(item)
        if select in names:
            self.cast_list.setCurrentRow(names.index(select))
        self.cast_list.blockSignals(False)
        self._update_buttons()

    def _selected(self) -> str | None:
        item = self.cast_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _update_buttons(self) -> None:
        selected = self._selected() is not None
        self.rename_btn.setEnabled(selected)
        self.export_btn.setEnabled(selected)
        self.insert_btn.setEnabled(self.cast_list.count() >= 2)

    # -- rename ------------------------------------------------------------- #
    def _rename(self) -> None:
        item = self.cast_list.currentItem()
        if item is not None:
            self.cast_list.editItem(item)

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        # Finish the edit before any message box or list change.
        QTimer.singleShot(0, lambda: self._apply_rename(item))

    def _apply_rename(self, item: QListWidgetItem) -> None:
        old = item.data(Qt.ItemDataRole.UserRole)
        new = item.text().strip()
        others = [n for n in load_casts() if n != old]
        error = cast_name_error(new, others) if new != old else None
        if error:
            QMessageBox.warning(self, "Rename Cast", error)
        elif new != old:
            rename_cast(old, new)
            self._panel.cast_renamed(old, new)
            old = new
        self.cast_list.blockSignals(True)
        item.setText(old)
        item.setData(Qt.ItemDataRole.UserRole, old)
        self.cast_list.blockSignals(False)

    # -- import, export, insert --------------------------------------------- #
    def _ready(self, cast: str) -> bool:
        """Before touching the panel's current cast, offer to save its
        unsaved changes. False if the user cancelled."""
        return cast != self._panel.cast_name() or self._panel.maybe_save_cast()

    def _merge(
        self,
        cast: str,
        incoming: list[Character],
        title: str,
        unusable: list[str] | None = None,
    ) -> None:
        if not self._ready(cast):
            return
        merged, report = merge_characters(
            load_casts().get(cast, []),
            incoming,
            ConflictResolver(cast, self),
        )
        save_cast(cast, merged)
        self._panel.casts_changed(cast)
        self._refresh(cast)
        QMessageBox.information(
            self, title, _summary(cast, report, unusable or [])
        )

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Cast", load_last_dir(), JSON_FILTER
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                incoming, unusable = characters_from_json(json.load(f))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            QMessageBox.critical(
                self, "Import Cast", f"Could not import {path}\n\n{exc}"
            )
            return
        save_last_dir(os.path.dirname(path))
        cast = self._selected()
        if cast is None:
            # With no casts yet, the file becomes one named after it.
            stem = os.path.splitext(os.path.basename(path))[0].strip()
            cast = _free_cast_name(stem or "Imported", list(load_casts()))
            save_cast(cast, [])
        self._merge(cast, incoming, "Import Cast", unusable)

    def _export(self) -> None:
        cast = self._selected()
        if cast is None or not self._ready(cast):
            return
        filename = cast.replace("/", "_").replace(os.sep, "_") + ".json"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Cast",
            os.path.join(load_last_dir(), filename),
            JSON_FILTER,
        )
        if not path:
            return
        data = cast_to_json(load_casts().get(cast, []))
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        except OSError as exc:
            QMessageBox.critical(
                self, "Export Cast", f"Could not save {path}\n\n{exc}"
            )
            return
        save_last_dir(os.path.dirname(path))

    def _insert(self) -> None:
        dialog = InsertCharactersDialog(load_casts(), self._selected(), self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        characters, cast = dialog.selection()
        dialog.deleteLater()
        if accepted and characters:
            self._merge(cast, characters, "Insert Characters")
