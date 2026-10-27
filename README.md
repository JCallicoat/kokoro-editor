# Kokoro Editor

A simple text editor with built-in text-to-speech playback through a
[Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) server (the
OpenAI-compatible `/v1/audio/speech` endpoint).

Audio is streamed as raw PCM, buffered in memory and played back locally, so
playback can be paused, resumed and scrubbed without touching the server.

## Install

```sh
pipx install .
```

Then run `kokoro-editor` (optionally with a file to open: `kokoro-editor notes.txt`).
You can also run it without installing via `python -m kokoro_editor`.

`sounddevice` needs the PortAudio library. It is bundled with the Windows and
macOS wheels; on Linux install it from your package manager (`portaudio` on
Arch, `libportaudio2` on Debian/Ubuntu).

## Usage

Open **File → Settings…** to set the server URL and choose up to three voices.
Each voice has a weight from 1 to 10; with equal weights the voices are mixed
evenly (`af_bella+am_puck`), otherwise the weights are sent with the names
(`af_bella(2)+am_puck(1)`).

**Play** reads the selected text, or the whole document if nothing is selected.

### Inline voice tags and control tokens

Every request is sent with `allow_voice_tags: true`, so you can switch voices
and insert pauses inline:

```
The narrator opens. [voice:af_bella] Did it land? [pause:0.3s] [voice:am_michael] It did.
```

This requires a server with voice tags enabled (the default). If the server
has `ENABLE_VOICE_TAGS=false`, the tags are read out as plain text instead.

### Keyboard shortcuts

| Action             | Shortcut          |
| ------------------ | ----------------- |
| New / Open / Save  | Ctrl+N / O / S    |
| Save As            | Ctrl+Shift+S      |
| Close document     | Ctrl+W            |
| Quit               | Ctrl+Q            |
| Play / Pause       | Ctrl+Space        |
| Stop               | Ctrl+.            |
| Scrub back / ahead | Alt+Left / Right  |
