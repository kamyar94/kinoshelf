"""The Windows-side script for folder icon attributes, run through powershell.exe -File (Windows only)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "windows" / "Set-FolderIconAttributes.ps1"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
pytestmark = pytest.mark.skipif(sys.platform != "win32" or not POWERSHELL, reason="needs Windows PowerShell")


def titled(root: Path, name: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "desktop.ini").write_text("[.ShellClassInfo]\r\nIconResource=FolderIcon_1.ico,0\r\n", encoding="utf-8")
    (folder / "FolderIcon_1.ico").write_bytes(b"\x00\x00\x01\x00")
    return folder


def run(*args: str) -> str:
    p = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), *args],
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    return p.stdout + p.stderr


def test_roots_as_one_comma_separated_string_through_file(tmp_path):
    a, b = tmp_path / "Movies A", tmp_path / "Movies B"
    titled(a, "Film One (2001)")
    titled(b, "Film Two (2002)")
    out = run("-Roots", f"{a},{b}", "-Days", "0", "-WhatIf")
    assert "Not found" not in out
    assert "2 folders looked at, 6 attribute changes (WhatIf: nothing changed)" in out


def test_a_root_whose_name_holds_a_comma_is_kept(tmp_path):
    odd = tmp_path / "Series, TV"
    titled(odd, "Show (2005)")
    out = run("-Roots", str(odd), "-Days", "0", "-WhatIf")
    assert "Not found" not in out and "1 folders looked at, 3 attribute changes" in out
