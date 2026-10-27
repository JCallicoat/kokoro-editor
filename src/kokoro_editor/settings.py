"""Persistent settings and small helpers shared by the UI."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from PySide6.QtCore import QSettings, QUrl

VOICE_SLOTS = 3
MAX_VOICE_WEIGHT = 10


def build_voice_string(voices: list[str], weights: list[int]) -> str:
    """Combine the chosen voices into Kokoro's voice-mix syntax.

    Equal weights give "af_bella+am_puck"; otherwise every voice carries its
    weight, e.g. "af_bella(2)+am_puck(1)". A lone voice is sent bare.
    """
    chosen = [(v, w) for v, w in zip(voices, weights) if v]
    if len(chosen) > 1 and len({w for _, w in chosen}) > 1:
        return "+".join(f"{v}({w})" for v, w in chosen)
    return "+".join(v for v, _ in chosen)


@dataclass
class AppSettings:
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

    @property
    def voice_string(self) -> str:
        return build_voice_string(self.voices, self.weights)

    @classmethod
    def load(cls) -> "AppSettings":
        s = QSettings()
        d = cls()
        slots = range(1, VOICE_SLOTS + 1)
        return cls(
            base_url=s.value("base_url", d.base_url, type=str),
            model=s.value("model", d.model, type=str),
            voices=[s.value(f"voice{i}", "", type=str) for i in slots],
            weights=[
                min(max(s.value(f"weight{i}", 1, type=int), 1), MAX_VOICE_WEIGHT)
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
        )

    def save(self) -> None:
        s = QSettings()
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
        s.sync()


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
