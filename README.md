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

- **Editor**: the editor font and size, the colours of tags that aren't
  characters, and whether to restore the previous session on startup (the last file and the last cast, each loaded on its own,
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
  at the current speed;
- a **colour** for its tags in the text. New characters get one unlike the
  others' (↻ suggests another), chosen to stand out on light and dark
  editors; tag text is black or white, whichever reads better.

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

- click the character's button on the tag toolbar under the editor;
- drag the character's row onto the editor;
- press **Alt+1** to **Alt+9** for the first nine characters (also listed
  under **Edit → Insert Character Tag**);
- press **Alt+0** to pick a character from any cast (picking from another
  cast switches to it); it remembers the last character picked in each cast.

Renaming a character renames its tags in the open document, as a single undo
step.

### Tags in the text

Tags are highlighted: a character's tags in its colour, raw voice mixes,
pauses, rate changes and pronunciations in colours set under
**Settings → Editor → Tag colours**. Unknown voice tags and tags that
validation would flag get a red wavy underline. Turn highlighting off with
**View → Highlight Tags** (Alt+Z).

Hover over a tag to see what it does (or what's wrong with it).
Double-click one to change it: pick another character for a voice tag
(optionally for every tag of that character), or a new length, rate or
pronunciation for the others. Drag a tag to move it, or Ctrl+drag to copy
it; moving one leaves no stray space behind. Tags don't need spaces around
them for any of this.

### Tag toolbar

The toolbar under the editor has a button for each character in the current
cast (names over 20 characters are shortened; if they don't all fit, arrows
or the mouse wheel scroll them), then buttons for the server's other tags:

- **Pause** asks for a length in seconds and inserts `[pause:1.5s]`;
- **Rate** asks for a speaking rate (until the next rate or voice tag) and
  inserts `[rate:1.2]`;
- **Pronunciation** asks for a word and its IPA and inserts
  `[Worcester](/wˈʊstər/)`, replacing the word if it's selected.

Hide or show it with **View → Tag Toolbar** (Alt+=); the choice is
remembered.

### Casts

A cast is a named, saved list of characters, shared by all documents. The
**Cast** row at the top of the panel switches between saved casts and has
buttons to create a new (empty) cast, delete the current one, and save its
changes (also **File → Save Cast**, Alt+S). You're asked whether to save unsaved changes before switching casts,
creating one, or quitting.

Saving a file remembers the cast it was saved with (for your recent files).
Opening it again while another cast is current offers to switch to that cast.

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
two. (The web UI skips characters that don't have a voice yet.) Character
colours are kept alongside, under a `kokoro_editor` key the web UI ignores.

### Checking tags

Before Play or Render, the text is checked for voice tags the server would
reject, which would fail the whole request: tags naming neither a character
in the current cast nor a valid voice mix. Tags with real voices, like
`[voice:am_michael(3)+af_bella(1)]`, are fine.

If any are found, you can replace them: for each unknown name, pick a
character (from any cast, importing it into the current cast if needed) and
every tag with that name is replaced. Tags you don't replace are left in the
document but removed from what's sent.

Play and Render also stop if the text uses characters that don't have a voice
yet, or if its pauses add up to more than the server allows in one request
(300 seconds by default).

**Edit → Validate Document Tags** checks the whole document, and runs by
itself whenever a file is opened, once the server's voice list has loaded
(staying quiet if all is well). Besides the
checks above, it lists tags that would be read aloud or quietly changed, with
a suggested fix where it's clear, and selects the first one:

- misspelt pauses, rate changes and voice tags, like `[pause:1.5]` (for
  `[pause:1.5s]`), `[pause: 2s]`, `[rate:fast]` or `[voice=bob]`;
- pronunciations without the slashes, like `[Worcester](wʊstər)`;
- pauses over 60 seconds, which the server shortens, and rates outside
  0.25–4×, which it limits.

Ordinary bracketed text like `[sic]` is left alone.

### Other inline tokens

Every request is sent with `allow_voice_tags: true`, so voice tags and the
server's other inline tokens work in the text: pauses (`[pause:0.3s]`), rate
changes (`[rate:1.2]`, until the next rate or voice tag) and pronunciations
(`[Worcester](/wˈʊstər/)`):

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
| Show / hide the tag toolbar      | Alt+=             |
| Save the current cast            | Alt+S             |
| Highlight tags on / off          | Alt+Z             |
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
