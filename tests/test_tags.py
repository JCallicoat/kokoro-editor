"""Checks for pause, rate, pronunciation and malformed voice tags."""

from __future__ import annotations

import pytest

from kokoro_editor.tags import pause_budget_error, tag_problems


@pytest.mark.parametrize(
    "text",
    [
        "[pause:1.5s] [PAUSE:2S] [pause:60s]",
        "[rate:1.2] [rate: 2 ] [RATE:0.25] [baserate:0.8]",
        "[voice:bob] [voice: Bob ] [voice:am_michael(3)+af_bella(1)]",
        "[Worcester](/wˈʊstər/)",
        "[sic] [1] [rate of exchange] [voices] [pause]",
        "[link](http://example.com) [a](mailto:x@y) [b](www.x) [c](#top)",
    ],
)
def test_valid_or_ordinary_text_passes(text):
    assert tag_problems(text) == []


@pytest.mark.parametrize(
    ("tag", "suggestion"),
    [
        ("[pause:1.5]", "[pause:1.5s]"),
        ("[pause: 2s]", "[pause:2s]"),
        ("[PAUSE 1.0]", "[pause:1.0s]"),
        ("[pause=3]", "[pause:3s]"),
        ("[rate:1.5x]", "[rate:1.5]"),
        ("[baserate=0.8]", "[baserate:0.8]"),
        ("[voice=bob]", "[voice:bob]"),
        ("[Worcester](wʊstər)", "[Worcester](/wʊstər/)"),
    ],
)
def test_tags_read_aloud_get_a_suggestion(tag, suggestion):
    (problem,) = tag_problems(f"Before {tag} after")
    assert "would be read aloud" in problem.message
    assert problem.message.endswith(f"Did you mean {suggestion}?")
    assert problem.start == len("Before ")
    assert problem.end == problem.start + len(tag)


@pytest.mark.parametrize("tag", ["[rate:fast]", "[voice:bob!]"])
def test_tags_read_aloud_without_a_clear_fix(tag):
    (problem,) = tag_problems(tag)
    assert "would be read aloud." in problem.message
    assert "Did you mean" not in problem.message


def test_values_the_server_adjusts():
    rate, pause = tag_problems("[rate:9] [pause:90s]")
    assert "kept within 0.25–4×" in rate.message
    assert "shortened to 60 seconds" in pause.message


def test_problems_are_in_text_order():
    problems = tag_problems("[x](y) [pause:1] [rate:9]")
    assert [p.start for p in problems] == [0, 7, 17]


def test_pause_budget():
    assert pause_budget_error("[pause:60s]" * 5) is None
    # Each pause counts up to 60 seconds.
    assert "add up to 360 seconds" in pause_budget_error("[pause:90s]" * 6)
