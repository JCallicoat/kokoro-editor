"""Characters: named voice mixes sent to Kokoro as voice_aliases and
referred to in the text with [voice:name] tags."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace

# Kokoro-FastAPI's web UI only imports cast names of this form, so sticking
# to it keeps the two interchangeable (its cast files are voice_aliases).
NAME_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,23}")
# Mirrors VOICE_TAG_PATTERN in Kokoro-FastAPI's text_processor.py.
VOICE_TAG_PATTERN = re.compile(
    r"\[voice:\s*([a-zA-Z0-9_][a-zA-Z0-9_+\-(). ]*?)\s*\]", re.IGNORECASE
)


# Drag and drop format carrying a character's name, so the editor can
# insert its tag the same way the shortcuts do.
CHARACTER_MIME_TYPE = "application/x-kokoro-editor-character"

# Kokoro-FastAPI's limits for speed and rates.
MIN_RATE = 0.25
MAX_RATE = 4.0


@dataclass
class Character:
    name: str
    voice: str = ""  # a voice mix string; "" until one is chosen
    # Multiplies the request speed whenever the character speaks. The server
    # reapplies it at each of the character's [voice:] tags.
    rate: float = 1.0

    def alias_value(self) -> str | dict:
        """The character's voice_aliases entry: the plain mix, or the mix
        with its rate, as the server and its web UI's cast files use."""
        if self.rate == 1.0:
            return self.voice
        return {"voice": self.voice, "rate": self.rate}

    def tooltip(self) -> str:
        return (
            f"{self.name}\n"
            f"Voice: {self.voice or '(none)'}\n"
            f"Rate: {self.rate:.2f}×"
        )


def clamp_rate(rate: float) -> float:
    return min(max(rate, MIN_RATE), MAX_RATE)


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


def voice_tag_pattern(name: str) -> str:
    """Regex for name's tags, spaced as the server allows. Match it
    case-insensitively, as the server does."""
    return rf"\[voice:\s*{re.escape(name)}\s*\]"


def voice_aliases(characters: list[Character]) -> dict[str, str | dict]:
    """The voice_aliases request field. Characters without a voice are left
    out, since the server rejects an alias with an empty mix."""
    return {c.name: c.alias_value() for c in characters if c.voice}


def cast_name_error(name: str, other_names: list[str]) -> str | None:
    """Why name can't be used for a cast, or None if it can."""
    if not name:
        return "Enter a name for the cast."
    if name.casefold() in {n.casefold() for n in other_names}:
        return f"There is already a cast named {name}."
    return None


# -- cast files ------------------------------------------------------------- #
# A cast file is the voice_aliases request field wrapped in an object, the
# format Kokoro-FastAPI's web UI exports and imports.
def cast_to_json(characters: list[Character]) -> dict:
    """A cast file's contents. Unlike requests, characters without a voice
    are kept (with an empty mix) so they survive a round trip; the web UI
    skips them."""
    return {"voice_aliases": {c.name: c.alias_value() for c in characters}}


def characters_from_json(data) -> tuple[list[Character], list[str]]:
    """Characters from a cast file, also accepting a bare voice_aliases
    object, and the names skipped as unusable (a bad name or rate, or a
    name repeated in another case).

    Raises ValueError if data isn't a cast file at all.
    """
    aliases = data.get("voice_aliases", data) if isinstance(data, dict) else 0
    if not isinstance(aliases, dict):
        raise ValueError("Expected a JSON object of voice aliases.")
    characters: list[Character] = []
    skipped: list[str] = []
    for raw_name, value in aliases.items():
        name = str(raw_name).strip()
        try:
            if isinstance(value, str):
                voice, rate = value.strip(), 1.0
            elif isinstance(value, dict):
                voice = str(value.get("voice", "")).strip()
                rate = float(value.get("rate", 1.0))
            else:
                raise ValueError(value)
        except (TypeError, ValueError):
            skipped.append(name)
            continue
        taken = [c.name for c in characters]
        if not MIN_RATE <= rate <= MAX_RATE or name_error(name, taken):
            skipped.append(name)
            continue
        characters.append(Character(name, voice, rate))
    return characters, skipped


@dataclass
class MergeReport:
    added: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    renamed: list[str] = field(default_factory=list)  # new names
    skipped: list[str] = field(default_factory=list)


def merge_characters(
    target: list[Character],
    incoming: list[Character],
    resolve: Callable[[Character, list[str]], tuple[str, str]],
) -> tuple[list[Character], MergeReport]:
    """Add incoming characters to target, asking resolve(character,
    taken_names) what to do about each name already taken (ignoring case):
    ("overwrite", _) replaces the existing character in place,
    ("rename", new_name) adds it as new_name, ("skip", _) leaves it out."""
    result = list(target)
    report = MergeReport()
    for character in incoming:
        taken = [c.name for c in result]
        folded = [n.casefold() for n in taken]
        if character.name.casefold() not in folded:
            result.append(character)
            report.added.append(character.name)
            continue
        action, new_name = resolve(character, taken)
        if action == "overwrite":
            result[folded.index(character.name.casefold())] = character
            report.replaced.append(character.name)
        elif action == "rename" and not name_error(new_name, taken):
            result.append(replace(character, name=new_name))
            report.renamed.append(new_name)
        else:
            report.skipped.append(character.name)
    return result, report


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
