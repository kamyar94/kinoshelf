"""Windows folder icons in the author's style, made inside the container (no Photoshop).

make_icon() takes a wide picture, finds its text and faces (detect.py), places it so the folder window is filled
edge to edge with titles and faces inside (filling a thin sliver from the picture's own edge only when nothing
else keeps the text whole), and draws the folder from the template layers (template.py).

The library names the icon FolderIcon_<IMDb id>.ico (FolderIcon_tmdbcoll<id>.ico for a collection folder,
FolderIcon_tt-unknown.ico / FolderIcon_noid.ico without an id) and points desktop.ini at it.
"""

from __future__ import annotations

import re

from PIL import Image

from . import detect, template

TITLED = ("keyart",)


def icon_name(ident: str | None, collection: bool = False) -> str:
    if collection:
        ident = f"tmdbcoll{ident}" if ident and str(ident).isdigit() else (ident if ident else "noid")
    elif not ident or not re.fullmatch(r"tt\d+", str(ident)):
        ident = "tt-unknown"
    return f"FolderIcon_{ident}.ico"


def desktop_ini(ico: str) -> bytes:
    """desktop.ini exactly as the library has it: ASCII, CRLF, three lines under [.ShellClassInfo]."""
    text = f"[.ShellClassInfo]\r\nIconResource={ico},0\r\nIconFile={ico}\r\nIconIndex=0\r\n"
    return text.encode("ascii", "replace")


def label_for(title: str, original: str | None = None) -> str:
    """The tab text: the title, or the original when the title is in Arabic script and the original is not."""
    t = title or ""
    if re.search(r"[؀-ۿ]", t) and original and not re.search(r"[؀-ۿ]", original):
        t = original
    return t.upper()


def make_icon(art: Image.Image, art_type: str = "keyart", label: str = "", cleans: list[Image.Image] | None = None,
              stack: int = 0) -> tuple[Image.Image, bytes, dict]:
    """(256 px PNG image, .ico bytes, report) for one picture."""
    art = art.convert("RGB")
    a = detect.analyze(art, cleans)
    p = detect.place(a, art_type)
    big = template.compose(art, p["box"], label, stack=stack, fill=p["method"] == "fill")
    png = template.to_png256(big)
    report = dict(method=p["method"], tier=p.get("tier"), scale=p["scale"], sliver=p["sliver"], cut=p["cut"],
                  text=len(a["text"]), faces=len(a["faces"]), diff=len(a["diff"]), box=p["box"],
                  detection=detect.models_ready())
    return png, template.ico_bytes(png), report


__all__ = ["desktop_ini", "icon_name", "label_for", "make_icon"]
