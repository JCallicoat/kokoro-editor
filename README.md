# Kokoro Editor

A simple text editor with built-in text-to-speech playback through a
[Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) server (the
OpenAI-compatible `/v1/audio/speech` endpoint), with named characters for
writing multi-speaker dialogue.

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

Characters and voice tags need Kokoro-FastAPI v0.8.0 or later, with voice tags
enabled on the server (the default).

## Usage

**Play** reads the selected text, or the whole document if nothing is selected.

**Render…** saves the same text to an audio file instead. Choose the file name
and one of the server's output formats (Opus, MP3, AAC, FLAC, WAV or raw PCM);
the extension follows the chosen format. The audio is generated in a single
non-streamed request, so long documents can take a while.

The window size, the last folder you opened or saved a file in, and your five
most recent files are remembered between sessions. Reopen recent files from
**File → Open Recent**.

### Settings

Open **File → Settings…**. Switch tabs with Alt and the tab's first letter.

- **Editor**: the editor font and size, and whether to restore the previous
  session on startup (the last file and the last cast, each loaded on its own,
  so a missing file doesn't stop the cast loading).
- **Kokoro**: the server URL and model, the default voice mix, speed, a
  server-side volume multiplier, and playback options (PCM sample rate,
  start-up buffer, and how far Alt+Left/Right scrubs).
- **Casts**: see [Managing casts](#managing-casts).

The default voice mix takes up to three voices. Each has a weight from 1 to
10; with equal weights the voices are mixed evenly (`af_bella+am_puck`),
otherwise the weights are sent with the names (`af_bella(2)+am_puck(1)`). The
play button beside each voice previews it, and the one beside **Resulting
voice** previews the whole mix; previews pause document playback while they
play.

### Characters

The **Characters** panel on the right (click its arrow, press Alt+-, or use
**View → Characters Panel**) holds the characters of the current cast. Each
character has:

- a **name** of letters, digits, `_` and `-` (not first), up to 24
  characters, unique ignoring case;
- a **voice mix**, chosen like the default one, or left empty to decide later;
- a **rate** from 0.25× to 4.00× that multiplies the speed setting whenever
  the character speaks. Hold Shift over the rate box to see the pace it gives
  at the current speed.

Use **Add** to create one, double-click a row or press Enter to edit it, and
**Remove** to delete it (you're warned if the document still uses it). Reorder
characters with the arrow buttons or by dragging rows within the table. Hover
over a row to see the character's full voice mix and rate.

Characters are sent to the server as `voice_aliases`, so in the text a
character is just a voice tag with its name:

```
[voice:narrator] It was a dark and stormy night. [voice:bob] Was it?
```

To insert a character's tag at the cursor:

- drag the character's row onto the editor;
- press **Alt+1** to **Alt+9** for the first nine characters (also listed
  under **Edit → Insert Character Tag**);
- press **Alt+0** to pick a character from any cast (picking from another
  cast switches to it); it remembers the last character picked in each cast.

Renaming a character renames its tags in the open document, as a single undo
step.

### Casts

A cast is a named, saved list of characters, shared by all documents. The
**Cast** row at the top of the panel switches between saved casts and has
buttons to create a new (empty) cast, delete the current one, and save its
changes. You're asked whether to save unsaved changes before switching casts,
creating one, or quitting.

#### Managing casts

The **Casts** tab in Settings lists the saved casts. Changes there are saved
straight away.

- **Rename** (or double-click, or F2) renames a cast in place.
- **Import…** adds the characters in a cast file to the selected cast.
- **Export…** saves the selected cast as a cast file.
- **Insert…** copies characters from one cast into another.

When an imported or inserted character's name is already taken, you can keep
the existing character, overwrite it, or rename the incoming one; Cancel skips
just that character, and you can apply your choice to all remaining conflicts.

Cast files are JSON in the `{"voice_aliases": {...}}` format that
Kokoro-FastAPI's web UI imports and exports, so casts can move between the
two. (The web UI skips characters that don't have a voice yet.)

### Checking voice tags

Before Play or Render, the text is checked for voice tags the server would
reject, which would fail the whole request: tags naming neither a character
in the current cast nor a valid voice mix. Tags with real voices, like
`[voice:am_michael(3)+af_bella(1)]`, are fine.

If any are found, you can replace them: for each unknown name, pick a
character (from any cast, importing it into the current cast if needed) and
every tag with that name is replaced. Tags you don't replace are left in the
document but removed from what's sent. **Edit → Validate Document Tags** runs
the same check on the whole document without speaking.

Characters that don't have a voice yet can't be spoken: if the text uses any,
Play and Render stop and list them so you can choose their voices.

### Other inline tokens

Every request is sent with `allow_voice_tags: true`, so voice tags and the
server's other inline tokens work in the text, for example a pause:

```
The narrator opens. [voice:af_bella] Did it land? [pause:0.3s] [voice:am_michael] It did.
```

If the server has `ENABLE_VOICE_TAGS=false`, tags are read out as plain text
instead.

### SSML

Check **SSML** next to the Stop button to send requests with `ssml: true`, so
the server interprets the text as SSML. The setting is remembered between
sessions.

### Keyboard shortcuts

| Action                           | Shortcut          |
| -------------------------------- | ----------------- |
| New / Open / Save                | Ctrl+N / O / S    |
| Save As                          | Ctrl+Shift+S      |
| Close document                   | Ctrl+W            |
| Quit                             | Ctrl+Q            |
| Play / Pause                     | Ctrl+Space        |
| Stop                             | Ctrl+.            |
| Scrub back / ahead               | Alt+Left / Right  |
| Insert character 1–9's tag       | Alt+1 … Alt+9     |
| Pick a character to insert       | Alt+0             |
| Show / hide the characters panel | Alt+-             |
| Edit the selected character      | Enter (in panel)  |

## Development

The tests use pytest and run without a display, a sound device or a Kokoro
server: Qt runs offscreen, the server and audio output are faked, and every
test gets its own empty settings, so your real settings and casts are never
touched.

```sh
pip install pytest   # also listed as the `test` dependency group
python -m pytest
```

Linting and formatting use [ruff](https://docs.astral.sh/ruff/), configured
in `pyproject.toml`:

```sh
ruff check
ruff format
```
