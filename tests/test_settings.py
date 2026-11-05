"""Settings, cast storage and request helpers."""

from __future__ import annotations

import json

import pytest
from PySide6.QtCore import QSettings

from kokoro_editor.characters import Character
from kokoro_editor.render_dialog import with_extension
from kokoro_editor.settings import (
    MAX_RECENT_FILES,
    AppSettings,
    add_recent_file,
    api_url,
    build_voice_string,
    delete_cast,
    describe_http_error,
    extract_voice_names,
    fmt_time,
    load_casts,
    load_last_cast,
    load_last_characters,
    load_recent_files,
    parse_voice_string,
    remove_recent_file,
    rename_cast,
    save_cast,
    save_last_cast,
    save_last_character,
    speech_payload,
)


@pytest.mark.parametrize(
    ("voices", "weights", "expected"),
    [
        (["af_bella", "", ""], [1, 1, 1], "af_bella"),
        (["af_bella", "am_puck", ""], [1, 1, 1], "af_bella+am_puck"),
        (["af_bella", "am_puck", ""], [2, 1, 1], "af_bella(2)+am_puck(1)"),
        (["af_bella", "", "am_puck"], [3, 9, 3], "af_bella+am_puck"),
        (["", "", ""], [1, 1, 1], ""),
    ],
)
def test_voice_strings_round_trip(voices, weights, expected):
    assert build_voice_string(voices, weights) == expected
    assert build_voice_string(*parse_voice_string(expected)) == expected


def test_parse_voice_string_pads_and_clamps():
    assert parse_voice_string("") == (["", "", ""], [1, 1, 1])
    assert parse_voice_string("a(99)+b(x)") == (["a", "b", ""], [10, 1, 1])


def test_settings_round_trip():
    settings = AppSettings(
        base_url="http://example:1234",
        voices=["af_sky", "am_puck", ""],
        weights=[2, 1, 1],
        speed=1.25,
        ssml=True,
        restore_session=False,
    )
    settings.save()
    assert AppSettings.load() == settings


def test_speech_payload():
    settings = AppSettings(voices=["af_sky", "", ""], speed=1.1)
    payload = speech_payload(settings, "Hi", "pcm", stream=True)
    assert payload["voice"] == "af_sky"
    assert payload["speed"] == 1.1
    assert payload["allow_voice_tags"] is True
    assert "voice_aliases" not in payload and "ssml" not in payload

    settings.ssml = True
    aliases = {"bob": {"voice": "am_puck", "rate": 0.8}}
    payload = speech_payload(
        settings, "Hi", "mp3", False, voice="x", voice_aliases=aliases
    )
    assert (payload["voice"], payload["ssml"]) == ("x", True)
    assert payload["voice_aliases"] == aliases


def test_api_url():
    assert api_url("localhost:8880/", "/v1/x").toString() == (
        "http://localhost:8880/v1/x"
    )
    assert api_url(" https://h ", "/v1/x").toString() == "https://h/v1/x"


def test_fmt_time():
    assert fmt_time(0) == "0:00"
    assert fmt_time(125.9) == "2:05"


def test_extract_voice_names():
    assert extract_voice_names(
        {"voices": [{"id": "b"}, {"name": "a"}, "c", {"x": 1}, "a"]}
    ) == ["a", "b", "c"]
    assert extract_voice_names(["z", "y"]) == ["y", "z"]


def test_describe_http_error():
    assert describe_http_error(400, b'{"detail": "bad voice"}') == (
        "HTTP 400: bad voice"
    )
    assert describe_http_error(500, b"") == "HTTP 500"
    assert describe_http_error(422, b'{"detail": [1]}') == "HTTP 422: [1]"


def test_with_extension():
    assert with_extension("/a/story", "mp3") == "/a/story.mp3"
    assert with_extension("/a/story.wav", "mp3") == "/a/story.mp3"
    assert with_extension("/a/story.v2", "mp3") == "/a/story.v2.mp3"


def test_recent_files(tmp_path):
    paths = [str(tmp_path / f"{n}.txt") for n in range(MAX_RECENT_FILES + 1)]
    for path in paths:
        add_recent_file(path)
    assert load_recent_files() == paths[::-1][:MAX_RECENT_FILES]
    add_recent_file(paths[2])
    assert load_recent_files()[0] == paths[2]
    remove_recent_file(paths[2])
    assert paths[2] not in load_recent_files()


# -- casts --------------------------------------------------------------- #
def test_casts_keep_their_order_and_rates(saga):
    save_cast("Other", [Character("x")])
    assert list(load_casts()) == ["Saga", "Other"]
    assert load_casts()["Saga"] == saga
    stored = json.loads(QSettings().value("casts"))
    # The default rate is left out, as in the web UI's cast files.
    assert stored[0]["characters"][1] == {"name": "bob", "voice": "am_puck"}
    assert stored[0]["characters"][0]["rate"] == 0.8


def test_casts_load_old_and_damaged_data():
    QSettings().setValue(
        "casts",
        json.dumps(
            [
                1,
                {"characters": []},
                {
                    "name": "Y",
                    "characters": [
                        {"name": "old", "voice": "af_sky"},
                        {"name": "bad", "rate": "fast"},
                        {"name": "big", "rate": 9},
                        {"voice": "nameless"},
                    ],
                },
            ]
        ),
    )
    assert load_casts() == {
        "Y": [
            Character("old", "af_sky"),
            Character("bad"),
            Character("big", "", 4.0),
        ]
    }
    QSettings().setValue("casts", "{not json")
    assert load_casts() == {}


def test_delete_cast(saga):
    delete_cast("Saga")
    delete_cast("missing")
    assert load_casts() == {}


def test_rename_cast_carries_what_is_remembered(saga):
    save_cast("Zed", [])
    save_last_cast("Saga")
    save_last_character("Saga", "bob")
    rename_cast("Saga", "Tale")
    assert list(load_casts()) == ["Tale", "Zed"]
    assert load_casts()["Tale"] == saga
    assert load_last_cast() == "Tale"
    assert load_last_characters() == {"Tale": "bob"}
