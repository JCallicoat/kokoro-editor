"""Persistent settings and small helpers shared by the UI."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from PySide6.QtCore import QByteArray, QSettings, QUrl
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtNetwork import QNetworkRequest

VOICE_SLOTS = 3
MAX_VOICE_WEIGHT = 10
MIN_FONT_SIZE = 6
MAX_FONT_SIZE = 72
REQUEST_INACTIVITY_TIMEOUT_MS = (
    120_000  # Qt 6 defaults to 30 s, too short for slow first chunks
)
VOICES_TIMEOUT_MS = 10_000


def build_voice_string(voices: list[str], weights: list[int]) -> str:
    """Combine the chosen voices into Kokoro's voice-mix syntax.

    Equal weights give "af_bella+am_puck"; otherwise every voice carries its
    weight, e.g. "af_bella(2)+am_puck(1)". A lone voice is sent bare.
    """
    chosen = [(v, w) for v, w in zip(voices, weights) if v]
    if len(chosen) > 1 and len({w for _, w in chosen}) > 1:
        return "+".join(f"{v}({w})" for v, w in chosen)
    return "+".join(v for v, _ in chosen)


def parse_voice_string(voice_string: str) -> tuple[list[str], list[int]]:
    """The inverse of build_voice_string, padded to VOICE_SLOTS entries."""
    voices = [""] * VOICE_SLOTS
    weights = [1] * VOICE_SLOTS
    parts = [p.strip() for p in voice_string.split("+") if p.strip()]
    for i, part in enumerate(parts[:VOICE_SLOTS]):
        name, _, weight = part.partition("(")
        voices[i] = name.strip()
        try:
            weights[i] = min(max(int(weight.rstrip(")")), 1), MAX_VOICE_WEIGHT)
        except ValueError:
            pass  # no weight given
    return voices, weights


@dataclass
class AppSettings:
    # Editor
    font_family: str = ""  # "" / 0 = the system default font
    font_size: int = 0
    # Kokoro
    base_url: str = "http://localhost:8880"
    model: str = "kokoro"
    voices: list[str] = field(default_factory=lambda: [""] * VOICE_SLOTS)
    weights: list[int] = field(default_factory=lambda: [1] * VOICE_SLOTS)
    speed: float = 1.0
    volume_multiplier: float = 1.0
    sample_rate: int = 24000
    prebuffer_ms: int = 400
    scrub_step_s: int = 5
    ssml: bool = False
    restore_session: bool = True

    @property
    def voice_string(self) -> str:
        return build_voice_string(self.voices, self.weights)

    def editor_font(self) -> QFont:
        font = QFont(QGuiApplication.font())
        if self.font_family:
            font.setFamily(self.font_family)
        if self.font_size > 0:
            font.setPointSize(self.font_size)
        return font

    @classmethod
    def load(cls) -> "AppSettings":
        s = QSettings()
        d = cls()
        slots = range(1, VOICE_SLOTS + 1)
        return cls(
            font_family=s.value("font_family", d.font_family, type=str),
            font_size=s.value("font_size", d.font_size, type=int),
            base_url=s.value("base_url", d.base_url, type=str),
            model=s.value("model", d.model, type=str),
            voices=[s.value(f"voice{i}", "", type=str) for i in slots],
            weights=[
                min(
                    max(s.value(f"weight{i}", 1, type=int), 1),
                    MAX_VOICE_WEIGHT,
                )
                for i in slots
            ],
            speed=s.value("speed", d.speed, type=float),
            volume_multiplier=s.value(
                "volume_multiplier", d.volume_multiplier, type=float
            ),
            sample_rate=s.value("sample_rate", d.sample_rate, type=int),
            prebuffer_ms=s.value("prebuffer_ms", d.prebuffer_ms, type=int),
            scrub_step_s=s.value("scrub_step_s", d.scrub_step_s, type=int),
            ssml=s.value("ssml", d.ssml, type=bool),
            restore_session=s.value(
                "restore_session", d.restore_session, type=bool
            ),
        )

    def save(self) -> None:
        s = QSettings()
        s.setValue("font_family", self.font_family)
        s.setValue("font_size", self.font_size)
        s.setValue("base_url", self.base_url)
        s.setValue("model", self.model)
        for i, (v, w) in enumerate(zip(self.voices, self.weights), start=1):
            s.setValue(f"voice{i}", v)
            s.setValue(f"weight{i}", w)
        s.setValue("speed", self.speed)
        s.setValue("volume_multiplier", self.volume_multiplier)
        s.setValue("sample_rate", self.sample_rate)
        s.setValue("prebuffer_ms", self.prebuffer_ms)
        s.setValue("scrub_step_s", self.scrub_step_s)
        s.setValue("ssml", self.ssml)
        s.setValue("restore_session", self.restore_session)
        s.sync()


# -- window/session state (not edited in the Settings dialog) --------------- #
MAX_RECENT_FILES = 5


def load_recent_files() -> list[str]:
    files = QSettings().value("recent_files", [], type=list)
    return [str(f) for f in files or [] if f]


def save_recent_files(files: list[str]) -> None:
    s = QSettings()
    s.setValue("recent_files", files)
    s.sync()


def add_recent_file(path: str) -> list[str]:
    """Move path to the top of the recent files list and return the new list."""
    path = os.path.abspath(path)
    files = [f for f in load_recent_files() if f != path]
    files = [path, *files][:MAX_RECENT_FILES]
    save_recent_files(files)
    return files


def remove_recent_file(path: str) -> list[str]:
    files = [f for f in load_recent_files() if f != path]
    save_recent_files(files)
    return files


def load_last_dir() -> str:
    return QSettings().value("last_dir", "", type=str)


def save_last_dir(directory: str) -> None:
    QSettings().setValue("last_dir", directory)


def load_window_geometry() -> QByteArray | None:
    geometry = QSettings().value("window_geometry")
    return geometry if isinstance(geometry, QByteArray) else None


def save_window_geometry(geometry: QByteArray) -> None:
    s = QSettings()
    s.setValue("window_geometry", geometry)
    s.sync()


def speech_payload(
    settings: AppSettings,
    text: str,
    response_format: str,
    stream: bool,
    voice: str | None = None,
    voice_aliases: dict[str, str] | None = None,
) -> dict:
    """JSON body for /v1/audio/speech, shared by playback and rendering."""
    payload = {
        "model": settings.model,
        "input": text,
        "voice": voice if voice is not None else settings.voice_string,
        "response_format": response_format,
        "speed": settings.speed,
        "volume_multiplier": settings.volume_multiplier,
        "stream": stream,
        "allow_voice_tags": True,  # inline [voice:...] and [pause:...] tokens
    }
    if settings.ssml:
        payload["ssml"] = True
    if voice_aliases:
        payload["voice_aliases"] = voice_aliases
    return payload


def speech_request(base_url: str, timeout_ms: int) -> QNetworkRequest:
    request = QNetworkRequest(api_url(base_url, "/v1/audio/speech"))
    request.setHeader(
        QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json"
    )
    request.setTransferTimeout(timeout_ms)
    return request


def voices_request(base_url: str) -> QNetworkRequest:
    request = QNetworkRequest(api_url(base_url, "/v1/audio/voices"))
    request.setTransferTimeout(VOICES_TIMEOUT_MS)
    return request


def api_url(base_url: str, path: str) -> QUrl:
    base = base_url.strip().rstrip("/")
    if "://" not in base:
        base = "http://" + base
    return QUrl(base + path)


def fmt_time(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}"


def extract_voice_names(payload) -> list[str]:
    """Accept {"voices": [...]} or a bare list; items may be strings or dicts."""
    items = payload.get("voices", []) if isinstance(payload, dict) else payload
    names: list[str] = []
    for item in items:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            name = item.get("id") or item.get("name") or item.get("voice_id")
            if name:
                names.append(str(name))
    return sorted(set(names))


def describe_http_error(status: int, body: bytes) -> str:
    detail = body.decode("utf-8", "replace").strip()
    try:
        data = json.loads(detail)
        if isinstance(data, dict):
            inner = data.get("detail", data.get("error", data))
            detail = inner if isinstance(inner, str) else json.dumps(inner)
    except ValueError:
        pass
    return f"HTTP {status}: {detail[:500]}" if detail else f"HTTP {status}"
