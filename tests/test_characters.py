"""Characters, voice tags and cast files (no Qt)."""

from __future__ import annotations

import json

import pytest

from kokoro_editor.characters import (
    CAST_FILE_KEY,
    Character,
    cast_name_error,
    cast_to_json,
    characters_from_json,
    clamp_rate,
    count_voice_tags,
    is_voice_mix,
    merge_characters,
    name_error,
    strip_voice_tags,
    unknown_tag_names,
    unvoiced_tag_names,
    voice_aliases,
    voice_tag,
)

VOICES = ["af_bella", "af_sky", "am_michael", "am_puck"]


def test_alias_value_carries_rate_only_when_not_1():
    assert Character("a", "af_sky").alias_value() == "af_sky"
    assert Character("a", "af_sky", 0.8).alias_value() == {
        "voice": "af_sky",
        "rate": 0.8,
    }


def test_tooltip():
    assert Character("bob", "", 1.25).tooltip() == (
        "bob\nVoice: (none)\nRate: 1.25×"
    )


def test_clamp_rate():
    assert clamp_rate(0.1) == 0.25
    assert clamp_rate(9) == 4.0
    assert clamp_rate(1.5) == 1.5


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("narrator", True),
        ("Bob_2", True),
        ("_x", True),
        ("a-b", True),
        ("x" * 24, True),
        ("", False),
        ("-bob", False),
        ("not a name", False),
        ("x" * 25, False),
        ("bob!", False),
    ],
)
def test_name_rules(name, ok):
    assert (name_error(name, []) is None) == ok


def test_names_are_unique_ignoring_case():
    assert (
        name_error("Bob", ["bob"]) == "There is already a character named Bob."
    )
    assert cast_name_error("saga", ["Saga"]) == (
        "There is already a cast named saga."
    )
    assert cast_name_error("", []) == "Enter a name for the cast."
    assert cast_name_error("My cast!", ["Saga"]) is None


def test_voice_aliases_leave_out_characters_without_a_voice():
    cast = [Character("a", "af_sky", 0.8), Character("b", "", 2.0)]
    assert voice_aliases(cast) == {"a": {"voice": "af_sky", "rate": 0.8}}


# -- cast files ---------------------------------------------------------- #
def test_cast_file_round_trip_keeps_voiceless_characters():
    cast = [
        Character("narrator", "af_sky(2)+am_puck(1)", 0.8),
        Character("bob", "am_puck"),
        Character("todo"),
    ]
    data = cast_to_json(cast)
    assert data["voice_aliases"] == {
        "narrator": {"voice": "af_sky(2)+am_puck(1)", "rate": 0.8},
        "bob": "am_puck",
        "todo": "",
    }
    # Default colours are written out, and read back as the characters'.
    colors = data[CAST_FILE_KEY]["colors"]
    assert list(colors) == ["narrator", "bob", "todo"]
    loaded, skipped = characters_from_json(json.loads(json.dumps(data)))
    assert skipped == []
    assert loaded == [
        Character(c.name, c.voice, c.rate, colors[c.name]) for c in cast
    ]


def test_cast_file_may_be_a_bare_alias_object():
    assert characters_from_json({"bob": "am_puck"}) == (
        [Character("bob", "am_puck")],
        [],
    )


def test_cast_file_skips_unusable_entries():
    data = {
        "voice_aliases": {
            "ok": "x",
            "not a name": "x",
            "Ok": "y",  # repeats "ok" in another case
            "fast": {"voice": "x", "rate": 9},
            "bad": {"voice": "x", "rate": "quick"},
            "nul": {"voice": "x", "rate": None},
            "num": 5,
            "  pad  ": "z",
            "slow": {"voice": "v", "rate": 0.5},
        }
    }
    assert characters_from_json(data) == (
        [
            Character("ok", "x"),
            Character("pad", "z"),
            Character("slow", "v", 0.5),
        ],
        ["not a name", "Ok", "fast", "bad", "nul", "num"],
    )


@pytest.mark.parametrize(
    "data", [[1, 2], "text", None, {"voice_aliases": [1]}]
)
def test_not_a_cast_file(data):
    with pytest.raises(ValueError, match="voice aliases"):
        characters_from_json(data)


# -- merging ------------------------------------------------------------- #
def test_merge_asks_only_about_taken_names():
    asked = []

    def resolve(character, taken):
        asked.append((character.name, taken))
        return ("skip", "")

    merged, report = merge_characters(
        [Character("bob", "a")],
        [Character("eve"), Character("BOB", "b")],
        resolve,
    )
    assert merged == [Character("bob", "a"), Character("eve")]
    assert asked == [("BOB", ["bob", "eve"])]
    assert (report.added, report.skipped) == (["eve"], ["BOB"])


def test_merge_actions():
    answers = iter(
        [("overwrite", ""), ("rename", "bob_2"), ("rename", "narrator")]
    )
    merged, report = merge_characters(
        [Character("bob", "a"), Character("narrator", "b")],
        [
            Character("Bob", "new"),
            Character("bob", "x"),
            Character("narrator"),
        ],
        lambda character, taken: next(answers),
    )
    # A rename to a taken name is skipped rather than added twice.
    assert merged == [
        Character("Bob", "new"),
        Character("narrator", "b"),
        Character("bob_2", "x"),
    ]
    assert report.replaced == ["Bob"]
    assert report.renamed == ["bob_2"]
    assert report.skipped == ["narrator"]


# -- tags ---------------------------------------------------------------- #
def test_voice_tag_counting_and_stripping():
    text = "[voice:bob] a [voice: BOB ] b [voice:bobby] c [voice:bob+af_sky]"
    assert voice_tag("bob") == "[voice:bob]"
    assert count_voice_tags(text, "bob") == 2
    assert count_voice_tags(text, "a.b") == 0  # regex characters escaped
    assert (
        strip_voice_tags(
            "Hello [voice:ghost] world\n[voice:Ghost]\nNext [voice: ghost ]",
            ["ghost"],
        )
        == "Hello world\n\nNext "
    )


@pytest.mark.parametrize(
    "mix",
    [
        "am_michael(3)+af_bella(1)",
        "af_sky",
        "af_bella-af_sky",
        "af_sky(0.5)+am_puck",
        " af_sky ( 2 ) ",
        "alloy",
        "nova+af_sky",
    ],
)
def test_voice_mixes_the_server_accepts(mix):
    assert is_voice_mix(mix, VOICES)


@pytest.mark.parametrize(
    "mix",
    [
        "nobody",
        "af_sky(0)",
        "af_sky(-1)",
        "af_sky(x)",
        "af_sky(inf)",
        "+af_sky",
        "af_sky+",
        "af_sky++am_puck",
        "af_sky(2",
        "af_sky((2))",
        "",
    ],
)
def test_voice_mixes_the_server_refuses(mix):
    assert not is_voice_mix(mix, VOICES)


def test_without_a_voice_list_only_the_form_is_checked():
    assert is_voice_mix("nobody(2)+someone", None)
    assert not is_voice_mix("a(0)", None)


def test_unknown_tag_names():
    cast = [Character("narrator", "af_sky"), Character("bob")]
    text = (
        "[voice:narrator] a [voice:NARRATOR] b "
        "[voice:am_michael(3)+af_bella(1)] c [voice:ghost] d "
        "[voice: Ghost ] e [voice:x y] [voice:bob]"
    )
    assert unknown_tag_names(text, cast, VOICES) == ["ghost", "x y"]
    assert unknown_tag_names("[voice:someone]", cast, None) == []


def test_unvoiced_tag_names():
    cast = [Character("narrator", "af_sky"), Character("bob")]
    assert unvoiced_tag_names(
        "[voice:narrator] [voice:BOB] [voice:bob]", cast
    ) == ["bob"]
