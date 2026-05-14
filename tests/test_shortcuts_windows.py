"""Windows shortcut helper behavior that can be tested on any OS."""
from pathlib import Path

from folder1004.shortcuts import (
    _build_lnk_powershell_command,
    _powershell_single_quoted,
    _windows_file_url,
)


def test_powershell_path_literals_escape_apostrophes():
    assert _powershell_single_quoted(r"C:\Users\O'Neil\문서.txt") == (
        r"'C:\Users\O''Neil\문서.txt'"
    )


def test_lnk_powershell_command_quotes_all_paths():
    target = Path(r"C:\Users\O'Neil\문서 #1.txt")
    lnk = Path(r"C:\Links\O'Neil shortcut.lnk")
    command = _build_lnk_powershell_command(target, lnk)

    assert "O''Neil" in command
    assert "$s.TargetPath =" in command
    assert "$s.WorkingDirectory =" in command
    assert "문서 #1.txt" in command


def test_windows_url_fallback_percent_encodes_special_paths():
    url = _windows_file_url(r"C:\Users\O'Neil\문서 #1 50%.txt")

    assert url == "file:///C:/Users/O%27Neil/%EB%AC%B8%EC%84%9C%20%231%2050%25.txt"
    assert " " not in url
    assert "#" not in url
