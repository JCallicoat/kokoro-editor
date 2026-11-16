"""Installing and removing the desktop entry and icon."""

from __future__ import annotations

import sys

import pytest

from kokoro_editor import desktop


@pytest.fixture
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    # Leave the real tool alone; it only refreshes the MIME cache.
    monkeypatch.setattr(desktop.shutil, "which", lambda _: None)
    return tmp_path


def test_data_home_ignores_relative_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", "relative/dir")
    assert desktop.data_home() == tmp_path / ".local" / "share"


def test_install_writes_entry_and_icon(data_home, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["/opt/bin/kokoro-editor"])
    assert desktop.install() == 0
    entry = data_home / "applications" / "kokoro-editor.desktop"
    icon = data_home / "icons/hicolor/scalable/apps/kokoro-editor.svg"
    text = entry.read_text()
    assert "Exec=/opt/bin/kokoro-editor %f\n" in text
    assert "Icon=kokoro-editor\n" in text
    assert icon.read_bytes() == desktop.icon_path().read_bytes()


def test_uninstall_removes_entry_and_icon(data_home, capsys):
    desktop.install()
    assert desktop.uninstall() == 0
    assert not desktop.desktop_file().exists()
    assert not desktop.installed_icon().exists()
    desktop.uninstall()
    assert "No desktop entry was installed." in capsys.readouterr().out


def test_launch_command_under_python_m(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["/src/kokoro_editor/__main__.py"])
    assert desktop.launch_command() == [
        sys.executable,
        "-m",
        "kokoro_editor",
    ]


@pytest.mark.parametrize(
    ("arg", "expected"),
    [
        ("/usr/bin/python", "/usr/bin/python"),
        ("/home/me/My Apps/kokoro-editor", '"/home/me/My Apps/kokoro-editor"'),
        ("/a$b", '"/a\\\\$b"'),
        ("/100%/x", "/100%%/x"),
    ],
)
def test_exec_quote(arg, expected):
    assert desktop.exec_quote(arg) == expected
