"""Colours for tags: default character colours that stand out on light and
dark editors, and readable text on any background (WCAG contrast)."""

from __future__ import annotations

import colorsys
import random
import re

_HEX = re.compile(r"#[0-9a-fA-F]{6}")
# Default colours aim for this relative luminance: about midway between
# white and black in contrast, so they show up against either background.
TARGET_LUMINANCE = 0.22
MIN_CONTRAST = 2.0  # against both white and black
SATURATION = 0.6


def is_color(value: object) -> bool:
    return isinstance(value, str) and bool(_HEX.fullmatch(value))


def _rgb(color: str) -> tuple[float, float, float]:
    return tuple(int(color[i : i + 2], 16) / 255 for i in (1, 3, 5))


def _hex(r: float, g: float, b: float) -> str:
    return "#" + "".join(f"{round(c * 255):02x}" for c in (r, g, b))


def luminance(color: str) -> float:
    """WCAG relative luminance, from 0 (black) to 1 (white)."""

    def linear(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(c) for c in _rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio, from 1 (none) to 21 (black on white)."""
    light, dark = sorted((luminance(a), luminance(b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def text_color(background: str) -> str:
    """Black or white, whichever reads better on background."""
    white, black = "#ffffff", "#000000"
    if contrast(background, black) >= contrast(background, white):
        return black
    return white


def color_for_hue(hue: float, saturation: float = SATURATION) -> str:
    """A colour of hue (0-360) with TARGET_LUMINANCE. Lightness is found
    per hue, since a yellow and a blue of the same lightness differ a lot
    in luminance."""
    low, high = 0.0, 1.0
    for _ in range(30):
        lightness = (low + high) / 2
        color = _hex(*colorsys.hls_to_rgb(hue / 360, lightness, saturation))
        if luminance(color) < TARGET_LUMINANCE:
            low = lightness
        else:
            high = lightness
    return color


def hue_of(color: str) -> float:
    return colorsys.rgb_to_hls(*_rgb(color))[0] * 360


def suggest_color(used: list[str], seed: object = None) -> str:
    """A colour unlike those in used: the hue furthest from theirs. seed
    makes the choice repeatable (e.g. a character's name); without one,
    hues equally far apart are picked at random."""
    rng = random.Random(seed) if seed is not None else random
    used_hues = [hue_of(c) for c in used if is_color(c)]
    candidates = list(range(0, 360, 5))
    rng.shuffle(candidates)
    if not used_hues:
        return color_for_hue(candidates[0])

    def distance(hue: float) -> float:
        return min(min(abs(hue - u), 360 - abs(hue - u)) for u in used_hues)

    return color_for_hue(max(candidates, key=distance))


# Defaults for the tags that aren't characters: muted, so characters stand
# out, and distinct from each other.
DEFAULT_PAUSE_COLOR = color_for_hue(200, 0.2)
DEFAULT_RATE_COLOR = color_for_hue(40, 0.35)
DEFAULT_PRONUNCIATION_COLOR = color_for_hue(280, 0.3)
DEFAULT_VOICE_MIX_COLOR = color_for_hue(0, 0.0)
