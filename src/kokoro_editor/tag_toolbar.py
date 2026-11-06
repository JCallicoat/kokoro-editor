"""Toolbar under the editor for inserting tags: one button per character
in the current cast, then pause, rate and pronunciation buttons. Each is
in its tag's colour (unless tags aren't highlighted in the text) and can
be clicked, or dragged to where the tag should go."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import (
    QByteArray,
    QEvent,
    QMimeData,
    QObject,
    QPoint,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QAction, QDrag, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from kokoro_editor.characters import (
    CHARACTER_MIME_TYPE,
    Character,
    character_colors,
    voice_tag,
)
from kokoro_editor.colors import text_color
from kokoro_editor.settings import AppSettings
from kokoro_editor.tags import pronunciation_error
from kokoro_editor.widgets import NEW_TAG_MIME_TYPE

# Longest character name shown on a button; longer ones are shortened.
MAX_LABEL_LENGTH = 20
# Narrowest the character strip gets, leaving room for its arrows.
CHARACTER_STRIP_MIN_WIDTH = 120
SCROLL_STEP = 60  # pixels per arrow click or wheel step
# Between every pair of buttons, in the strip and on the toolbar. Not 2:
# at fractional display scales, gaps round a pixel either way, which shows
# less on a wider gap.
BUTTON_SPACING = 3


def button_label(name: str) -> str:
    """A character's button text: its name, shortened to MAX_LABEL_LENGTH
    characters with an ellipsis, and & escaped so Qt doesn't treat it as a
    mnemonic marker."""
    if len(name) > MAX_LABEL_LENGTH:
        name = name[: MAX_LABEL_LENGTH - 1] + "…"
    return name.replace("&", "&&")


def button_style(color: str | None) -> str:
    """A tag button's style sheet: in its tag colour, as in the text, or
    plain for None."""
    if color is None:
        return ""
    return (
        f"QToolButton {{ background-color: {color};"
        f" color: {text_color(color)}; border-radius: 3px;"
        " padding: 2px 6px; }"
        " QToolButton:hover { border: 1px solid palette(highlight); }"
    )


class DragStarter(QObject):
    """Lets a button be dragged as well as clicked: pressing it and moving
    far enough drags mime_data() instead of clicking it."""

    def __init__(
        self, button: QAbstractButton, mime_data: Callable[[], QMimeData]
    ) -> None:
        super().__init__(button)
        self._mime_data = mime_data
        self._pressed: QPoint | None = None
        button.installEventFilter(self)

    def eventFilter(self, button: QObject, event: QEvent) -> bool:
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress:
            left = event.button() == Qt.MouseButton.LeftButton
            self._pressed = event.position().toPoint() if left else None
        elif kind == QEvent.Type.MouseButtonRelease:
            self._pressed = None
        elif (
            kind == QEvent.Type.MouseMove
            and self._pressed is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            moved = event.position().toPoint() - self._pressed
            if moved.manhattanLength() >= QApplication.startDragDistance():
                self._drag(button)
                return True
        return False

    def _drag(self, button: QAbstractButton) -> None:
        pressed, self._pressed = self._pressed, None
        # Not down, so letting go over the button doesn't click it too.
        button.setDown(False)
        drag = QDrag(button)
        drag.setMimeData(self._mime_data())
        drag.setPixmap(button.grab())
        drag.setHotSpot(pressed)
        drag.exec(Qt.DropAction.CopyAction)


def character_mime_data(name: str) -> QMimeData:
    """What dragging a character's button carries: its name for the
    editor, as the panel's rows do, and its tag for other apps."""
    data = QMimeData()
    data.setData(CHARACTER_MIME_TYPE, QByteArray(name.encode()))
    data.setText(voice_tag(name))
    return data


def new_tag_mime_data(kind: str) -> QMimeData:
    """What dragging a pause, rate or pronunciation button carries: the
    kind of tag, which the editor asks for once it's dropped."""
    data = QMimeData()
    data.setData(NEW_TAG_MIME_TYPE, QByteArray(kind.encode()))
    return data


class CharacterStrip(QWidget):
    """A row of character buttons as wide as they need, given the room;
    when they don't fit, arrow buttons (and the wheel) scroll them."""

    character_clicked = Signal(str)  # character name

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Up to as wide as its buttons (its size hint), shrinking when the
        # toolbar has less room; not Preferred, which would let the toolbar
        # stretch it past its buttons.
        self.setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred
        )
        self.left_btn = self._arrow(Qt.ArrowType.LeftArrow, -SCROLL_STEP)
        self.right_btn = self._arrow(Qt.ArrowType.RightArrow, SCROLL_STEP)

        self._buttons_widget = QWidget()
        self._buttons_layout = QHBoxLayout(self._buttons_widget)
        self._buttons_layout.setContentsMargins(0, 0, 0, 0)
        self._buttons_layout.setSpacing(BUTTON_SPACING)
        # The row is at least as wide as its buttons, so when they don't fit
        # the scroll range follows them as they're added or shown.
        self._buttons_layout.setSizeConstraint(
            QLayout.SizeConstraint.SetMinimumSize
        )
        self._buttons_layout.addStretch(1)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setWidget(self._buttons_widget)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        # Its built-in minimum (68px) would keep a short cast's strip wider
        # than its buttons; an explicit one overrides it.
        self.scroll_area.setMinimumWidth(1)
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.scroll_area.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.scroll_area.horizontalScrollBar().valueChanged.connect(
            self._update_arrow_states
        )
        self.scroll_area.horizontalScrollBar().rangeChanged.connect(
            self._update_arrow_states
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.left_btn)
        layout.addWidget(self.scroll_area, 1)
        layout.addWidget(self.right_btn)
        self.buttons: list[QToolButton] = []
        self._colors: list[str] = []  # each character button's colour
        self._colored = True
        self._fitting = False
        self.set_characters([])

    def _arrow(self, arrow: Qt.ArrowType, step: int) -> QToolButton:
        button = QToolButton()
        button.setArrowType(arrow)
        button.setAutoRaise(True)
        button.setAutoRepeat(True)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(lambda: self.scroll_by(step))
        return button

    def scroll_by(self, pixels: int) -> None:
        bar = self.scroll_area.horizontalScrollBar()
        bar.setValue(bar.value() + pixels)

    def set_characters(self, characters: list[Character]) -> None:
        for button in self.buttons:
            self._buttons_layout.removeWidget(button)
            # Hidden now: deleteLater() waits for the event loop, and until
            # then the old button would still show and take up room.
            button.hide()
            button.deleteLater()
        self.buttons = []
        colors = character_colors(characters)
        self._colors = [colors[c.name.casefold()] for c in characters]
        for character in characters:
            button = QToolButton()
            button.setText(button_label(character.name))
            button.setToolTip(character.tooltip())
            button.setWhatsThis(character.whats_this())
            button.setAutoRaise(True)
            DragStarter(
                button,
                lambda name=character.name: character_mime_data(name),
            )
            # Leave focus in the editor.
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(
                lambda _=False, name=character.name: (
                    self.character_clicked.emit(name)
                )
            )
            self.buttons.append(button)
        self._style_buttons()
        if not characters:
            empty = QToolButton()
            empty.setText("No characters")
            empty.setAutoRaise(True)
            empty.setEnabled(False)
            self.buttons.append(empty)
        # Before the stretch that keeps them left-aligned. Shown now rather
        # than when control returns to the event loop, so the row's size is
        # right straight away.
        for index, button in enumerate(self.buttons):
            self._buttons_layout.insertWidget(index, button)
            button.show()
        self._resize_to_buttons()
        self.scroll_area.horizontalScrollBar().setValue(0)

    def set_colored(self, colored: bool) -> None:
        """Show the character buttons in their tag colours, or plain (which
        sizes them differently)."""
        self._colored = colored
        self._style_buttons()
        self._resize_to_buttons()

    def _resize_to_buttons(self) -> None:
        """Size the strip to its buttons after they change."""
        self.scroll_area.setFixedHeight(
            max(button.sizeHint().height() for button in self.buttons)
        )
        self._buttons_layout.activate()
        # Room for the arrows and some buttons, unless the buttons need less.
        self.setMinimumWidth(
            min(self._content_width(), CHARACTER_STRIP_MIN_WIDTH)
        )
        self.updateGeometry()
        self._fit()
        # Fit again once the toolbar has given the strip its new width,
        # which may not change it (so no resize event) when it was already
        # as wide as the toolbar allows. With self as the context, the timer
        # is dropped if the strip is deleted first.
        QTimer.singleShot(0, self, self._fit)

    def _style_buttons(self) -> None:
        for button, color in zip(self.buttons, self._colors, strict=False):
            button.setStyleSheet(
                button_style(color if self._colored else None)
            )

    def _content_width(self) -> int:
        return self._buttons_layout.minimumSize().width()

    def _fit(self) -> None:
        """Show the arrows only if the buttons need more than the whole
        strip, then size the row to the room left. (The scroll area grows
        the row but doesn't shrink it by itself.)

        Showing or hiding an arrow resizes things and so calls this again
        from resizeEvent; that nested call is skipped, or it could leave
        one arrow shown and the other hidden.
        """
        if self._fitting:
            return
        self._fitting = True
        try:
            overflowing = self._content_width() > self.width()
            for arrow in (self.left_btn, self.right_btn):
                arrow.setVisible(overflowing)
            self.layout().activate()  # so the viewport has its new width
            viewport = self.scroll_area.viewport()
            self._buttons_widget.resize(
                max(viewport.width(), self._content_width()),
                viewport.height(),
            )
            self._update_arrow_states()
        finally:
            self._fitting = False

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit()

    def _update_arrow_states(self) -> None:
        bar = self.scroll_area.horizontalScrollBar()
        self.left_btn.setEnabled(bar.value() > bar.minimum())
        self.right_btn.setEnabled(bar.value() < bar.maximum())

    def sizeHint(self) -> QSize:
        # All the buttons, with no arrows: as much room as is needed.
        return QSize(self._content_width(), super().sizeHint().height())

    def wheelEvent(self, event: QWheelEvent) -> None:
        # Either wheel direction scrolls the row sideways.
        delta = event.angleDelta()
        steps = (delta.y() or delta.x()) / 120
        self.scroll_by(int(-steps * SCROLL_STEP))
        event.accept()


class TagToolbar(QToolBar):
    """Asks for tags rather than inserting them, so the window can insert
    them the same way as its other insertion methods. Dragged, the buttons
    leave the editor to ask (see DragStarter)."""

    character_clicked = Signal(str)  # character name
    pause_clicked = Signal()
    rate_clicked = Signal()
    pronunciation_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Tags", parent)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        # As the strip spaces its buttons; some styles (Breeze) leave none
        # between toolbar items. (Not a style sheet, which would drop the
        # style's margins around the toolbar.)
        self.layout().setSpacing(BUTTON_SPACING)
        self.strip = CharacterStrip()
        self.strip.character_clicked.connect(self.character_clicked)
        self.addWidget(self.strip)
        self.addSeparator()
        # Tag kind -> its button, to colour.
        self.tag_buttons: dict[str, QAbstractButton] = {}
        self._tag_colors: dict[str, str] = {}  # tag kind -> colour
        self._colored = True
        self._tag_action(
            "pause",
            "Pause…",
            "Insert a pause of a chosen length: [pause:1.5s]",
            self.pause_clicked,
        )
        self._tag_action(
            "rate",
            "Rate…",
            "Change the speaking rate until the next rate or voice tag: "
            "[rate:1.2]",
            self.rate_clicked,
        )
        self._tag_action(
            "pronunciation",
            "Pronunciation…",
            "Say a word (the selected one, if any) as written in IPA: "
            "[word](/ipa/)",
            self.pronunciation_clicked,
        )

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        # A new style resets the spacing to its own.
        if event.type() == QEvent.Type.StyleChange:
            self.layout().setSpacing(BUTTON_SPACING)

    def _tag_action(self, kind: str, text: str, tooltip: str, signal) -> None:
        action = QAction(text, self)
        action.setToolTip(tooltip)
        action.triggered.connect(lambda _=False: signal.emit())
        self.addAction(action)
        button = self.widgetForAction(action)
        button.setAutoRaise(True)
        DragStarter(button, lambda: new_tag_mime_data(kind))
        self.tag_buttons[kind] = button

    def set_characters(self, characters: list[Character]) -> None:
        self.strip.set_characters(characters)

    def set_tag_colors(self, settings: AppSettings) -> None:
        """Colour the pause, rate and pronunciation buttons as settings
        colours their tags."""
        self._tag_colors = {
            kind: getattr(settings, f"{kind}_color")
            for kind in self.tag_buttons
        }
        self._style_tag_buttons()

    def set_colored(self, colored: bool) -> None:
        """Show every button in its tag's colour, or plain (as the text is
        when tags aren't highlighted)."""
        self._colored = colored
        self.strip.set_colored(colored)
        self._style_tag_buttons()

    def _style_tag_buttons(self) -> None:
        for kind, button in self.tag_buttons.items():
            color = self._tag_colors.get(kind) if self._colored else None
            button.setStyleSheet(button_style(color))


class PronunciationDialog(QDialog):
    """Asks for a word and how to say it in IPA."""

    def __init__(
        self, word: str, ipa: str, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Insert Pronunciation")
        self.setMinimumWidth(320)
        self.word_edit = QLineEdit(word)
        self.ipa_edit = QLineEdit(ipa)
        self.ipa_edit.setPlaceholderText("e.g. wˈʊstər")
        hint = QLabel(
            "The word is spoken as the IPA instead (English only). "
            "Slashes around the IPA are added for you."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        form = QFormLayout()
        form.addRow("Word:", self.word_edit)
        form.addRow("IPA:", self.ipa_edit)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(buttons)
        # A selected word only needs its IPA.
        (self.ipa_edit if word else self.word_edit).setFocus()

    def values(self) -> tuple[str, str]:
        """The word and IPA, trimmed, without slashes typed around the
        IPA."""
        return (
            self.word_edit.text().strip(),
            self.ipa_edit.text().strip().strip("/").strip(),
        )

    def accept(self) -> None:
        error = pronunciation_error(*self.values())
        if error:
            QMessageBox.warning(self, "Insert Pronunciation", error)
            return
        super().accept()
