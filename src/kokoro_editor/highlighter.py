"""Colours tags in the editor: characters' tags in their colours, other
tags in the colours set in Settings, and problems underlined."""

from __future__ import annotations

from PySide6.QtGui import (
    QColor,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextDocument,
)

from kokoro_editor.characters import Character, is_voice_mix
from kokoro_editor.colors import text_color
from kokoro_editor.settings import AppSettings
from kokoro_editor.tags import find_tags, tag_problems, utf16_index

PROBLEM_COLOR = "#e0245e"


def _tag_format(background: str) -> QTextCharFormat:
    format_ = QTextCharFormat()
    format_.setBackground(QColor(background))
    format_.setForeground(QColor(text_color(background)))
    return format_


class TagHighlighter(QSyntaxHighlighter):
    """Highlights tags, given the current cast's characters and colours,
    the server's voices and the settings (update() refreshes them)."""

    def __init__(self, document: QTextDocument) -> None:
        super().__init__(document)
        self._characters: set[str] = set()  # casefolded names
        self._colors: dict[str, str] = {}
        self._voices: list[str] | None = None
        self._kind_formats: dict[str, QTextCharFormat] = {}
        self.update([], {}, None, AppSettings())

    def update(
        self,
        characters: list[Character],
        colors: dict[str, str],
        voices: list[str] | None,
        settings: AppSettings,
    ) -> None:
        self._characters = {c.name.casefold() for c in characters}
        self._colors = dict(colors)
        self._voices = voices
        self._kind_formats = {
            "pause": _tag_format(settings.pause_color),
            "rate": _tag_format(settings.rate_color),
            "baserate": _tag_format(settings.rate_color),
            "pronunciation": _tag_format(settings.pronunciation_color),
            "mix": _tag_format(settings.voice_mix_color),
        }
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:
        def utf16(index: int) -> int:
            return utf16_index(text, index)

        def underline(start: int, end: int) -> None:
            # On top of whatever the text already has, e.g. a tag colour.
            first, last = utf16(start), utf16(end)
            format_ = QTextCharFormat(self.format(first))
            format_.setUnderlineStyle(
                QTextCharFormat.UnderlineStyle.WaveUnderline
            )
            format_.setUnderlineColor(QColor(PROBLEM_COLOR))
            self.setFormat(first, last - first, format_)

        unknown = []
        for tag in find_tags(text):
            if tag.kind == "voice":
                key = tag.value.casefold()
                if key in self._characters:
                    format_ = _tag_format(self._colors[key])
                elif is_voice_mix(tag.value, self._voices):
                    format_ = self._kind_formats["mix"]
                else:
                    unknown.append(tag)
                    continue
            else:
                format_ = self._kind_formats[tag.kind]
            start = utf16(tag.start)
            self.setFormat(start, utf16(tag.end) - start, format_)
        for tag in unknown:
            underline(tag.start, tag.end)
        for problem in tag_problems(text):
            underline(problem.start, problem.end)
