"""Characters: named voice mixes sent to Kokoro as voice_aliases and
referred to in the text with [voice:name] tags."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Kokoro-FastAPI's web UI only imports cast names of this form, so sticking
# to it keeps the two interchangeable (its cast files are voice_aliases).
NAME_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,23}")
# Mirrors VOICE_TAG_PATTERN in Kokoro-FastAPI's text_processor.py.
VOICE_TAG_PATTERN = re.compile(
    r"\[voice:\s*([a-zA-Z0-9_][a-zA-Z0-9_+\-(). ]*?)\s*\]", re.IGNORECASE
)


@dataclass
class Character:
    name: str
    voice: str = ""  # a voice mix string; "" until one is chosen


def name_error(name: str, other_names: list[str]) -> str | None:
    """Why name can't be used for a character, or None if it can.

    The server matches aliases case-insensitively, so names that differ
    only in case would collide.
    """
    if not name:
        return "Enter a name for the character."
    if not NAME_PATTERN.fullmatch(name):
        return (
            "Names can use letters, digits, _ and - (not first), "
            "up to 24 characters."
        )
    if name.casefold() in {n.casefold() for n in other_names}:
        return f"There is already a character named {name}."
    return None


def voice_tag(name: str) -> str:
    return f"[voice:{name}]"


def voice_aliases(characters: list[Character]) -> dict[str, str]:
    """The voice_aliases request field. Characters without a voice are left
    out, since the server rejects an alias with an empty mix."""
    return {c.name: c.voice for c in characters if c.voice}


def unvoiced_tag_names(text: str, characters: list[Character]) -> list[str]:
    """Characters tagged in text that have no voice yet, which the server
    would reject as unknown voices."""
    unvoiced = {c.name.casefold(): c.name for c in characters if not c.voice}
    found: dict[str, str] = {}
    for match in VOICE_TAG_PATTERN.finditer(text):
        key = match.group(1).casefold()
        if key in unvoiced:
            found.setdefault(key, unvoiced[key])
    return list(found.values())
