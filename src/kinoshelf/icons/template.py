"""The folder icon look: a picture inside a folder-shaped window, a coloured tab with the title, a faint tint.

The shapes come from the author's own Photoshop template (512 x 512), exported once into ``assets/``:

  back.png        the folder back, as rendered (10 % opacity)
  tab_mask.png    the tab shape; the tab is filled with a dark tint of the picture's average colour
  panel_mask.png  the window shape; the picture is masked by it, edge to edge
  front.png       the window's tint, as rendered (10 % opacity), laid over picture and text

Layer order, bottom to top, as in the template: stacked folders (collections only), back, tab, picture, title,
front. The 512 px result is reduced to 256 px and written as a multi-size .ico (256 to 16 px, PNG frames).
"""

from __future__ import annotations

import colorsys
import io
import os
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

SIZE = 512
OUT_SIZE = 256
ICO_SIZES = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
PANEL = (0, 112, 512, 430)  # the window's box in template pixels
TEXT_CENTER_X = 135  # centre of the raised part of the tab
TEXT_BASELINE = 75  # bottom of the title's letters
TEXT_CAP = 15  # capital height
TEXT_MAXW = 146  # usable width of the raised tab part
TEXT_MINCAP = 10  # below this a subtitle after ":" is dropped
ASSETS = Path(__file__).with_name("assets")


def asset_dir() -> Path:
    """Where the downloaded extras (font, detection models) live: KS_ICON_ASSETS, else the package folder."""
    return Path(os.environ.get("KS_ICON_ASSETS") or ASSETS)


@lru_cache(maxsize=None)
def _layer(name: str, mode: str) -> Image.Image:
    im = Image.open(ASSETS / name)
    im.load()
    return im.convert(mode)


def tab_colour(img: Image.Image) -> tuple[int, int, int]:
    """A dark tint of the picture's average colour; white text reads on it at every icon size."""
    im = img.convert("RGB").resize((64, 64))
    px = list(im.getdata())
    r, g, b = (sum(c[i] for c in px) / len(px) / 255 for i in range(3))
    h, _l, s = colorsys.rgb_to_hls(r, g, b)
    r, g, b = colorsys.hls_to_rgb(h, 0.17, min(s, 0.55))
    return round(r * 255), round(g * 255), round(b * 255)


@lru_cache(maxsize=4)
def _font_file() -> str | None:
    for name in ("Oswald-Bold.ttf", "Oswald[wght].ttf", "Oswald.ttf"):
        p = asset_dir() / name
        if p.exists():
            return str(p)
    return None


def _font(px: float) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = _font_file()
    size = max(4, int(round(px)))
    if path:
        f = ImageFont.truetype(path, size)
        try:
            if b"Bold" in f.get_variation_names():
                f.set_variation_by_name("Bold")
        except (OSError, AttributeError):
            pass  # a static font has no variations
        return f
    return ImageFont.load_default(size)


def _fit_text(label: str) -> tuple[ImageFont.ImageFont, tuple[int, int, int, int]]:
    """Font size so the letters are TEXT_CAP high and at most TEXT_MAXW wide (the template's rule)."""
    size = 20.0
    for _ in range(4):
        box = _font(size).getbbox(label)
        w, h = box[2] - box[0], box[3] - box[1]
        if w <= 0 or h <= 0:
            break
        f = min(TEXT_CAP / h, TEXT_MAXW / w)
        if abs(f - 1) < 0.02:
            break
        size *= f
    font = _font(size)
    return font, font.getbbox(label)


def _title(canvas: Image.Image, label: str, colour=(255, 255, 255)) -> None:
    label = (label or "").strip()
    if not label:
        return
    font, box = _fit_text(label)
    if box[3] - box[1] < TEXT_MINCAP and ":" in label:
        label = label.split(":")[0].strip()
        font, box = _fit_text(label)
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x = TEXT_CENTER_X - (box[0] + box[2]) / 2
    y = TEXT_BASELINE - box[3]
    d.text((x, y), label, font=font, fill=colour + (255,))
    canvas.alpha_composite(layer)


def _tab(colour: tuple[int, int, int]) -> Image.Image:
    layer = Image.new("RGBA", (SIZE, SIZE), colour + (0,))
    layer.putalpha(_layer("tab_mask.png", "L"))
    return layer


def _stack(colour: tuple[int, int, int], n: int) -> Image.Image:
    """Collection cue: n folder outlines behind the folder, offset up and right, in lighter tab shades."""
    shape = Image.new("L", (SIZE, SIZE), 0)
    shape.paste(255, (0, 0), _layer("panel_mask.png", "L"))
    shape.paste(255, (0, 0), _layer("tab_mask.png", "L"))
    out = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    for k in range(n, 0, -1):
        f = 0.22 + 0.16 * k
        c = tuple(round(v + (255 - v) * f) for v in colour)
        sil = Image.new("RGBA", (SIZE, SIZE), c + (0,))
        moved = Image.new("L", (SIZE, SIZE), 0)
        moved.paste(shape, (9 * k, -11 * k))
        sil.putalpha(moved)
        out.alpha_composite(sil)
    return out


def place_picture(art: Image.Image, box: tuple[float, float, float, float]) -> Image.Image:
    """The picture scaled into box (template px; may reach past the canvas), masked by the window."""
    x0, y0, x1, y1 = box
    w, h = max(1, int(round(x1 - x0))), max(1, int(round(y1 - y0)))
    pic = art.convert("RGB").resize((w, h), Image.LANCZOS)
    layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    layer.paste(pic.convert("RGBA"), (int(round(x0)), int(round(y0))))
    mask = _layer("panel_mask.png", "L")
    alpha = Image.new("L", (SIZE, SIZE), 0)
    alpha.paste(layer.getchannel("A"), (0, 0), mask)
    layer.putalpha(alpha)
    return layer


def mirror_fill(art: Image.Image, box: tuple[float, float, float, float]) -> Image.Image:
    """The picture at its place with the thin uncovered slivers of the window filled from its own mirrored edge,
    slightly blurred (sliver only). Returns a window-sized picture (2x scale) for place_picture(..., PANEL)."""
    import numpy as np

    k = 2.0
    pw, ph = int((PANEL[2] - PANEL[0]) * k), int((PANEL[3] - PANEL[1]) * k)
    bw, bh = int(round((box[2] - box[0]) * k)), int(round((box[3] - box[1]) * k))
    im = art.convert("RGB").resize((max(1, bw), max(1, bh)), Image.LANCZOS)
    ox, oy = int(round((box[0] - PANEL[0]) * k)), int(round((box[1] - PANEL[1]) * k))
    left, top = max(0, ox), max(0, oy)
    right, bottom = max(0, pw - (ox + bw)), max(0, ph - (oy + bh))
    arr = np.asarray(im)
    padded = np.pad(arr, ((top, bottom), (left, right), (0, 0)), mode="symmetric")
    full = Image.fromarray(padded)
    blurred = full.filter(ImageFilter.GaussianBlur(4))
    mask = Image.new("L", full.size, 255)
    mask.paste(0, (left, top, left + bw, top + bh))
    full.paste(blurred, (0, 0), mask)
    cx, cy = max(0, -ox), max(0, -oy)
    return full.crop((cx, cy, cx + pw, cy + ph))


def compose(art: Image.Image, box, label: str, colour: tuple[int, int, int] | None = None, stack: int = 0,
            fill: bool = False) -> Image.Image:
    """The 512 px icon. box: where the picture goes (template px), from detect.place()."""
    colour = colour or tab_colour(art)
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    if stack:
        canvas.alpha_composite(_stack(colour, stack))
    canvas.alpha_composite(_layer("back.png", "RGBA"))
    canvas.alpha_composite(_tab(colour))
    if fill:
        canvas.alpha_composite(place_picture(mirror_fill(art, box), PANEL))
    else:
        canvas.alpha_composite(place_picture(art, box))
    _title(canvas, label.upper())
    return _front_tint(canvas)


def _front_tint(canvas: Image.Image) -> Image.Image:
    """The template's "front" layer: its colour at about 10 % over the window, with the red channel left out of
    the blend (the layer's own blending setting, measured by rendering it over flat colours in Photoshop): green
    and blue move towards the layer's colour, red stays as it is."""
    import numpy as np

    front = np.asarray(_layer("front.png", "RGBA"), dtype=np.float32)
    a = front[..., 3:4] / 255.0
    px = np.asarray(canvas, dtype=np.float32).copy()
    for ch in (1, 2):
        px[..., ch] = px[..., ch] * (1 - a[..., 0]) + front[..., ch] * a[..., 0]
    return Image.fromarray(np.clip(px + 0.5, 0, 255).astype(np.uint8), "RGBA")


def to_png256(icon512: Image.Image) -> Image.Image:
    return icon512.resize((OUT_SIZE, OUT_SIZE), Image.LANCZOS)


def ico_bytes(png256: Image.Image) -> bytes:
    buf = io.BytesIO()
    png256.convert("RGBA").save(buf, format="ICO", sizes=ICO_SIZES)
    return buf.getvalue()
