"""Wide (16:9) art for folder icons: never a portrait poster.

Order (the author's rules): TMDB key art with an English title, a clean backdrop without text, key art in the
title's original language, any other backdrop in any language, a TV episode still, and last a frame of the video
itself (black bars cut off). Within a group: images at least 1920 px wide first, then votes, then width.
"""

from __future__ import annotations

import hashlib
import io
import logging
import subprocess
from pathlib import Path

import httpx
from PIL import Image

log = logging.getLogger(__name__)
API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/"


class Tmdb:
    """A small TMDB client for images, with a file cache for downloaded pictures."""

    def __init__(self, api_key: str, proxy: str | None = None, cache: Path | None = None, timeout: float = 20):
        self.key = (api_key or "").strip()
        self.cache = cache
        self.client = httpx.Client(proxy=proxy or None, trust_env=False, timeout=timeout, follow_redirects=True)

    def get(self, path: str, **params) -> dict | None:
        if not self.key:
            return None
        headers = {"Accept": "application/json"}
        if self.key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {self.key}"
        else:
            params["api_key"] = self.key
        try:
            r = self.client.get(API + path, params=params, headers=headers)
        except httpx.HTTPError as e:
            log.info("TMDB %s: %s", path, type(e).__name__)
            return None
        return r.json() if r.status_code == 200 else None

    def image(self, file_path: str, size: str = "w1280") -> Image.Image | None:
        url = IMG + size + file_path
        cp = None
        if self.cache is not None:
            cp = self.cache / (hashlib.sha1(url.encode()).hexdigest() + Path(file_path).suffix)
            if cp.exists():
                return Image.open(cp).convert("RGB")
        try:
            r = self.client.get(url)
        except httpx.HTTPError:
            return None
        if r.status_code != 200 or not r.content:
            return None
        if cp is not None:
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_bytes(r.content)
        return Image.open(io.BytesIO(r.content)).convert("RGB")

    def close(self) -> None:
        self.client.close()


def _landscape(b: dict) -> bool:
    return (b.get("width") or 0) > (b.get("height") or 0)


def _best(group: list[dict]) -> dict | None:
    return max(group, key=lambda b: (b.get("width", 0) >= 1920, b.get("vote_average", 0), b.get("vote_count", 0),
                                     b.get("width", 0))) if group else None


def choose(tm: Tmdb, kind: str, tmdb_id: str | int, language: str = "") -> dict | None:
    """{"type", "path", "width", "height", "cleans"} from TMDB, or None. kind: "movie" or "tv"."""
    tk = "tv" if kind in ("tv", "series") else "movie"
    langs = "en,null" + ("," + language if language and language != "en" else "")
    imgs = tm.get(f"/{tk}/{tmdb_id}/images", include_image_language=langs) or {}
    backs = [b for b in imgs.get("backdrops") or [] if _landscape(b)]
    wide = [b for b in backs if 1.70 <= (b.get("aspect_ratio") or 0) <= 1.80] or backs
    cleans = sorted([b for b in wide if not b.get("iso_639_1")], key=lambda b: -(b.get("vote_average") or 0))
    order = [("keyart", _best([b for b in wide if b.get("iso_639_1") == "en"])),
             ("clean", _best([b for b in wide if not b.get("iso_639_1")])),
             ("keyart_" + language, _best([b for b in wide if language and language != "en"
                                           and b.get("iso_639_1") == language]))]
    for t, b in order:
        if b:
            return {"type": t, "path": b["file_path"], "width": b["width"], "height": b["height"],
                    "cleans": [c["file_path"] for c in cleans[:3] if c is not b]}
    anyb = [b for b in (tm.get(f"/{tk}/{tmdb_id}/images") or {}).get("backdrops") or [] if _landscape(b)]
    titled = _best([b for b in anyb if b.get("iso_639_1")])
    if titled:
        return {"type": "keyart_" + titled["iso_639_1"], "path": titled["file_path"], "width": titled["width"],
                "height": titled["height"], "cleans": []}
    clean = _best([b for b in anyb if not b.get("iso_639_1")])
    if clean:
        return {"type": "clean", "path": clean["file_path"], "width": clean["width"], "height": clean["height"],
                "cleans": []}
    if tk == "tv":
        d = tm.get(f"/tv/{tmdb_id}") or {}
        seasons = sorted(s.get("season_number") for s in d.get("seasons") or [] if s.get("season_number")) or [0]
        for sn in seasons[:3]:
            season = tm.get(f"/tv/{tmdb_id}/season/{sn}") or {}
            stills = []
            for ep in (season.get("episodes") or [])[:8]:
                im = tm.get(f"/tv/{tmdb_id}/season/{sn}/episode/{ep.get('episode_number', 0)}/images") or {}
                stills += [x for x in im.get("stills") or [] if _landscape(x)]
            if stills:
                b = _best(stills)
                return {"type": "still", "path": b["file_path"], "width": b["width"], "height": b["height"], "cleans": []}
    return None


def collection_art(tm: Tmdb, cid: str | int) -> dict | None:
    """A TMDB collection's backdrop, titled (English) first. Landscape only."""
    imgs = tm.get(f"/collection/{cid}/images", include_image_language="en,null") or {}
    backs = [b for b in imgs.get("backdrops") or [] if _landscape(b)]
    for t, b in (("keyart", _best([x for x in backs if x.get("iso_639_1") == "en"])),
                 ("clean", _best([x for x in backs if not x.get("iso_639_1")]))):
        if b:
            return {"type": t, "path": b["file_path"], "width": b["width"], "height": b["height"], "cleans": []}
    return None


def download(tm: Tmdb, art: dict) -> tuple[Image.Image | None, list[Image.Image]]:
    size = "w1280" if (art.get("width") or 0) >= 1280 else "original"
    img = tm.image(art["path"], size)
    cleans = [c for c in (tm.image(p, "w300") for p in art.get("cleans") or []) if c is not None]
    return img, cleans


def video_frame(video: Path, duration_s: float | None, timeout: int = 60) -> Image.Image | None:
    """A well-lit frame of the video itself (35 %, 50 %, 65 % or 20 % in), black bars cut off. Landscape only."""
    from .detect import crop_bars

    if not duration_s:
        return None
    best = None
    for p in (0.35, 0.5, 0.65, 0.2):
        try:
            out = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{duration_s * p:.2f}", "-i", str(video),
                                  "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                                 capture_output=True, timeout=timeout, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if not out:
            continue
        im = crop_bars(Image.open(io.BytesIO(out)).convert("RGB"))
        g = im.convert("L")
        stat = g.resize((64, 36))
        px = list(stat.getdata())
        mean = sum(px) / len(px)
        spread = (sum((v - mean) ** 2 for v in px) / len(px)) ** 0.5
        if im.width > im.height and 40 <= mean <= 210:
            score = spread
            if best is None or score > best[0]:
                best = (score, im)
    return best[1] if best else None
