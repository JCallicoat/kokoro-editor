"""Kokoro-FastAPI's inline tokens besides character tags (pauses, rate
changes and pronunciations), and checks for tags the server would read
aloud, adjust, or refuse."""

from __future__ import annotations

import re
from dataclasses import dataclass

from kokoro_editor.characters import MAX_RATE, MIN_RATE, VOICE_TAG_PATTERN

# Kokoro-FastAPI's defaults for MAX_PAUSE_DURATION_S and MAX_TOTAL_PAUSE_S.
MAX_PAUSE_S = 60.0
MAX_TOTAL_PAUSE_S = 300.0

# These mirror PAUSE_TAG_PATTERN, CONTROL_TAG_PATTERN and CUSTOM_PHONEMES in
# Kokoro-FastAPI's text_processor.py, which decide what really is a tag.
PAUSE_PATTERN = re.compile(r"\[pause:(\d+(?:\.\d+)?)s\]", re.IGNORECASE)
RATE_PATTERN = re.compile(
    r"\[(?:base)?rate:\s*(\d+(?:\.\d+)?)\s*\]", re.IGNORECASE
)
PRONUNCIATION_PATTERN = re.compile(r"\[[^\[\]]*?\]\(\/[^\/\(\)]*?\/\)")

# Bracketed text that starts like a tag (a keyword, then a separator or a
# number), valid or not. Ordinary brackets like [sic] or [1] don't match.
_TAG_LIKE = re.compile(
    r"\[\s*(voice|rate|baserate|pause)\s*(?:[:=]|\d)[^\[\]\n]*\]",
    re.IGNORECASE,
)
# Bracketed text followed by parentheses, like a pronunciation.
_PRONUNCIATION_LIKE = re.compile(r"\[([^\[\]\n]+)\]\(([^()\n]*)\)")
# What a Markdown-style link points at, which isn't a pronunciation.
_LINK = re.compile(r"[a-z][a-z0-9+.-]*:|www\.|#", re.IGNORECASE)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def pause_tag(seconds: float) -> str:
    return f"[pause:{seconds:g}s]"


def rate_tag(rate: float) -> str:
    return f"[rate:{rate:g}]"


def pronunciation_tag(word: str, ipa: str) -> str:
    return f"[{word}](/{ipa}/)"


def pronunciation_error(word: str, ipa: str) -> str | None:
    """Why word and ipa can't make a pronunciation tag, or None if they
    can (the server's pattern allows no brackets in the word, nor slashes
    or parentheses in the IPA)."""
    if not word.strip():
        return "Enter the word to pronounce."
    if re.search(r"[\[\]]", word):
        return "The word can't contain square brackets."
    if not ipa.strip():
        return "Enter its pronunciation in IPA."
    if re.search(r"[/()]", ipa):
        return "The IPA can't contain slashes or parentheses."
    return None


@dataclass
class TagProblem:
    start: int  # index of the tag in the text
    end: int
    message: str


def _read_aloud(tag: str, kind: str, suggestion: str | None) -> str:
    message = (
        f"{tag} isn't a {kind} the server understands, so it would be "
        "read aloud."
    )
    if suggestion and suggestion != tag:
        message += f" Did you mean {suggestion}?"
    return message


def _check_tag(tag: str, keyword: str) -> str | None:
    """What's wrong with a tag-like piece of text, if anything."""
    number = _NUMBER.search(tag)
    if keyword == "voice":
        # Whether the voice exists is checked separately.
        if VOICE_TAG_PATTERN.fullmatch(tag):
            return None
        name = re.sub(
            r"^\[\s*voice\s*[:=]?\s*", "", tag[:-1], flags=re.IGNORECASE
        )
        suggestion = f"[voice:{name.strip()}]"
        if not VOICE_TAG_PATTERN.fullmatch(suggestion):
            suggestion = None
        return _read_aloud(tag, "voice tag", suggestion)
    if keyword == "pause":
        match = PAUSE_PATTERN.fullmatch(tag)
        if match is None:
            suggestion = f"[pause:{number.group()}s]" if number else None
            return _read_aloud(tag, "pause", suggestion)
        if float(match.group(1)) > MAX_PAUSE_S:
            return (
                f"{tag} is longer than the server allows, so it's "
                f"shortened to {MAX_PAUSE_S:g} seconds."
            )
        return None
    match = RATE_PATTERN.fullmatch(tag)
    if match is None:
        suggestion = f"[{keyword}:{number.group()}]" if number else None
        return _read_aloud(tag, "rate change", suggestion)
    if not MIN_RATE <= float(match.group(1)) <= MAX_RATE:
        return (
            f"{tag} is outside the rates the server allows, so it's kept "
            f"within {MIN_RATE:g}–{MAX_RATE:g}×."
        )
    return None


def tag_problems(text: str) -> list[TagProblem]:
    """Pause, rate and pronunciation tags (and malformed voice tags) the
    server would read aloud or quietly adjust, in text order."""
    problems = []
    for match in _TAG_LIKE.finditer(text):
        message = _check_tag(match.group(0), match.group(1).lower())
        if message:
            problems.append(TagProblem(match.start(), match.end(), message))
    for match in _PRONUNCIATION_LIKE.finditer(text):
        word, ipa = match.group(1), match.group(2).strip().strip("/")
        if PRONUNCIATION_PATTERN.fullmatch(match.group(0)) or _LINK.match(ipa):
            continue
        problems.append(
            TagProblem(
                match.start(),
                match.end(),
                f"{match.group(0)} isn't a pronunciation the server "
                "understands (the IPA goes between slashes), so it would be "
                f"read aloud. Did you mean [{word}](/{ipa}/)?",
            )
        )
    return sorted(problems, key=lambda problem: problem.start)


def pause_budget_error(text: str) -> str | None:
    """Why the server would refuse text for its pauses, if it would: their
    total, each counted up to the longest allowed, is capped."""
    total = sum(
        min(float(match.group(1)), MAX_PAUSE_S)
        for match in PAUSE_PATTERN.finditer(text)
    )
    if total <= MAX_TOTAL_PAUSE_S:
        return None
    return (
        f"The pauses add up to {total:g} seconds, more than the "
        f"{MAX_TOTAL_PAUSE_S:g} the server allows in one request, so it "
        "would refuse it."
    )
