"""What may be published. Pictures in the repository are limited to the README screenshots (demo data), the app's
own icon and the four folder-icon template layers. Generated folder icons, posters, backdrops, comparison sheets
and any library art never belong here; neither do VPN share links."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PICTURES = re.compile(r"\.(png|jpe?g|webp|gif|bmp|tiff?|ico|icns|psd|heic|avif|svg)$", re.I)
ALLOWED = {
    "docs/screenshots/review.png", "docs/screenshots/review-phone.png", "docs/screenshots/settings.png",
    "docs/screenshots/updates.png", "docs/screenshots/week.png", "docs/screenshots/filing.png",
    "docs/screenshots/playback.png", "docs/screenshots/playback-settings.png",
    "src/kinoshelf/web/static/icon.svg",
    # the folder-icon template's four shape layers (no art, no titles)
    "src/kinoshelf/icons/assets/back.png", "src/kinoshelf/icons/assets/front.png",
    "src/kinoshelf/icons/assets/panel_mask.png", "src/kinoshelf/icons/assets/tab_mask.png",
}


def tracked() -> list[str]:
    if not shutil.which("git") or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [p for p in out.split("\0") if p]


def test_only_known_pictures_are_tracked():
    extra = sorted(p for p in tracked() if PICTURES.search(p) and p not in ALLOWED)
    assert not extra, f"pictures that may not be published: {extra}"


def test_no_share_links_or_folder_icon_files_are_tracked():
    files = tracked()
    assert not [p for p in files if Path(p).name.lower() in ("desktop.ini",) or p.lower().startswith("route/")]
    link = re.compile(r"\b(vless|vmess|trojan|ss|hysteria2?|tuic)://[^\s'\"`]{12,}", re.I)
    for p in files:
        f = ROOT / p
        if f.suffix.lower() in (".png", ".jpg", ".ico", ".woff2", ".ttf", ".onnx", ".gz") or not f.is_file():
            continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        assert not link.search(text), f"{p} holds what looks like a share link"
