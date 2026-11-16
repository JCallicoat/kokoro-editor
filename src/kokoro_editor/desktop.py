"""Install the desktop entry and icon into the user's XDG data directory,
which pip and pipx leave out."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

APP_ID = "kokoro-editor"

# Characters that make an Exec argument need quoting, from the Desktop Entry
# Specification.
_RESERVED = set(" \t\n\"'\\><~|&;$*?#()`")


def icon_path() -> Path:
    return Path(
        str(resources.files("kokoro_editor") / "icons" / f"{APP_ID}.svg")
    )


def data_home() -> Path:
    # The spec says to ignore a relative XDG_DATA_HOME.
    value = os.environ.get("XDG_DATA_HOME", "")
    if os.path.isabs(value):
        return Path(value)
    return Path.home() / ".local" / "share"


def desktop_file() -> Path:
    return data_home() / "applications" / f"{APP_ID}.desktop"


def installed_icon() -> Path:
    return (
        data_home()
        / "icons"
        / "hicolor"
        / "scalable"
        / "apps"
        / f"{APP_ID}.svg"
    )


def launch_command() -> list[str]:
    """The command that started this process, as a launcher should run it.

    The kokoro-editor script is kept by the path it was found at (pipx's
    symlink survives reinstalls); otherwise, e.g. under python -m, the
    interpreter runs the module."""
    script = sys.argv[0]
    if os.path.basename(script) == APP_ID:
        return [os.path.abspath(script)]
    return [sys.executable, "-m", "kokoro_editor"]


def exec_quote(arg: str) -> str:
    """Quote one Exec argument, escaped for a desktop file string value."""
    if any(c in _RESERVED for c in arg):
        for c in '\\"`$':
            arg = arg.replace(c, "\\" + c)
        arg = f'"{arg}"'
    # A literal % is %%, and the string value itself escapes backslashes.
    return arg.replace("%", "%%").replace("\\", "\\\\")


def desktop_entry(command: list[str]) -> str:
    exec_line = " ".join(exec_quote(arg) for arg in command)
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Kokoro Editor\n"
        "GenericName=Text-to-Speech Editor\n"
        "Comment=Write text and hear it read by a Kokoro TTS server\n"
        f"Exec={exec_line} %f\n"
        f"Icon={APP_ID}\n"
        "Terminal=false\n"
        "Categories=Utility;TextEditor;\n"
        "MimeType=text/plain;\n"
        "Keywords=TTS;text-to-speech;speech;Kokoro;voice;\n"
    )


def _refresh_database() -> None:
    # Updates the "Open With" lists; desktops notice the file without it.
    tool = shutil.which("update-desktop-database")
    if tool:
        subprocess.run(
            [tool, str(desktop_file().parent)],
            check=False,
            capture_output=True,
        )


def install() -> int:
    entry, icon = desktop_file(), installed_icon()
    entry.parent.mkdir(parents=True, exist_ok=True)
    icon.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(icon_path(), icon)
    entry.write_text(desktop_entry(launch_command()), encoding="utf-8")
    _refresh_database()
    print(f"Installed {entry}\nInstalled {icon}")
    return 0


def uninstall() -> int:
    removed = False
    for path in (desktop_file(), installed_icon()):
        if path.exists():
            path.unlink()
            print(f"Removed {path}")
            removed = True
    if removed:
        _refresh_database()
    else:
        print("No desktop entry was installed.")
    return 0
