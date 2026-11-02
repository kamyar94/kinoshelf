"""Content detection and placement for folder icons.

analyze(image, cleans) finds what must stay visible in the picture:
  text   a DB scene-text detector (PP-OCRv3 detection model from the OpenCV model zoo), axis-aligned boxes
  diff   when a textless version of the same backdrop is known, the area where the two differ (the title)
  faces  the YuNet face detector (OpenCV model zoo)
  sal    spectral-residual saliency on a 64 x 36 grid (tie-breaker for the main subject)
Both models run with onnxruntime; without it (or without the model files) the boxes are simply empty and the
picture is centred.

place(analysis) picks scale and offset so the picture fills the folder window edge to edge and every kept box
(text and title boxes at least 2 % of the picture height; faces at least 4 %, the five largest and only those at
least half the size of the largest) stays inside the window's outline, clear of its tapered sides and rounded
corners. When no crop can do that, the picture is made just small enough to keep the text inside and the thin
uncovered slivers are filled from its own edge (method "fill"); faces are dropped from the constraint before that.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache

import numpy as np
from PIL import Image, ImageFilter

from .template import PANEL, asset_dir

log = logging.getLogger(__name__)

TEXT_MODEL = "text_detection_en_ppocrv3_2023may.onnx"
FACE_MODEL = "face_detection_yunet_2023mar.onnx"
TEXT_MIN_H, FACE_MIN_H, MARGIN = 0.02, 0.04, 0.008  # fractions of the picture height
EDGE_M = 4.0  # kept boxes stay this far inside the window outline (template px)


# ---------------------------------------------------------------------------------------------- models
@lru_cache(maxsize=None)
def _session(name: str):
    path = asset_dir() / name
    if not path.exists():
        log.info("Icon detection model %s not found; detection skipped", name)
        return None
    os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")  # the app reaches the internet only for lookups
    try:
        import onnxruntime as ort
    except ImportError:
        log.info("onnxruntime not installed; icon detection skipped")
        return None
    try:
        ort.disable_telemetry_events()
    except AttributeError:
        pass
    ort.set_default_logger_severity(3)
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 2
    opts.log_severity_level = 3
    return ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])


def models_ready() -> bool:
    return _session(TEXT_MODEL) is not None and _session(FACE_MODEL) is not None


# ---------------------------------------------------------------------------------------------- helpers
def _label(mask: np.ndarray) -> list[tuple[int, int, int, int, int, np.ndarray]]:
    """8-connected components of a boolean mask by row runs.
    Returns (x0, y0, x1, y1, area, (rows, cols) index arrays) per component; x1/y1 exclusive."""
    parent: list[int] = []

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    runs = []  # (row, start, end) per run id
    prev: list[int] = []
    for y in range(mask.shape[0]):
        row = mask[y]
        if not row.any():
            prev = []
            continue
        d = np.diff(np.concatenate(([0], row.view(np.int8), [0])))
        starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
        cur = []
        j = 0
        for s, e in zip(starts.tolist(), ends.tolist()):
            rid = len(runs)
            runs.append((y, s, e))
            parent.append(rid)
            while j < len(prev) and runs[prev[j]][2] < s:  # previous run ends before this one starts (8-conn: e >= s)
                j += 1
            k = j
            while k < len(prev) and runs[prev[k]][1] <= e:  # previous run starts at or before this end (8-conn)
                ra, rb = find(rid), find(prev[k])
                if ra != rb:
                    parent[ra] = rb
                k += 1
            cur.append(rid)
        prev = cur
    comps: dict[int, list[int]] = {}
    for rid in range(len(runs)):
        comps.setdefault(find(rid), []).append(rid)
    out = []
    for ids in comps.values():
        ys, xs = [], []
        x0 = y0 = 10 ** 9
        x1 = y1 = -1
        area = 0
        for rid in ids:
            y, s, e = runs[rid]
            x0, x1, y0, y1 = min(x0, s), max(x1, e), min(y0, y), max(y1, y + 1)
            area += e - s
            ys.append(np.full(e - s, y))
            xs.append(np.arange(s, e))
        out.append((x0, y0, x1, y1, area, (np.concatenate(ys), np.concatenate(xs))))
    return out


def _gauss_kernel(size: int, sigma: float) -> np.ndarray:
    x = np.arange(size) - (size - 1) / 2
    k = np.exp(-(x ** 2) / (2 * sigma * sigma))
    return k / k.sum()


def _conv_sep(a: np.ndarray, k: np.ndarray) -> np.ndarray:
    """Separable filter with the kernel k on both axes, mirrored borders (OpenCV's default border)."""
    r = len(k) // 2
    p = np.pad(a, r, mode="reflect")
    tmp = sum(k[i] * p[:, i:i + a.shape[1]] for i in range(len(k)))  # rows: (H + 2r) x W
    return sum(k[i] * tmp[i:i + a.shape[0], :] for i in range(len(k)))


def saliency(gray: Image.Image) -> np.ndarray:
    """Spectral-residual saliency on a 64 x 36 grid, 0..1."""
    g = np.asarray(gray.resize((64, 36), Image.BOX), dtype=np.float32)
    f = np.fft.fft2(g)
    la = np.log(np.abs(f) + 1e-6)
    box = np.ones(3) / 3
    sr = la - _conv_sep(la, box)
    s = np.abs(np.fft.ifft2(np.exp(sr + 1j * np.angle(f)))) ** 2
    s = _conv_sep(s, _gauss_kernel(5, 2.0))
    return (s / (s.max() + 1e-9)).round(3)


# ---------------------------------------------------------------------------------------------- detectors
def text_boxes(img: Image.Image) -> list[list]:
    sess = _session(TEXT_MODEL)
    if sess is None:
        return []
    w, h = img.size
    iw = 736
    ih = max(96, int(round(iw * h / w / 32.0)) * 32)
    x = np.asarray(img.convert("RGB").resize((iw, ih), Image.BILINEAR), dtype=np.float32)
    x = (x - np.array([122.67891434, 116.66876762, 104.00698793], np.float32)) / 255.0
    x = x.transpose(2, 0, 1)[None]
    prob = np.asarray(sess.run(None, {sess.get_inputs()[0].name: x})[0], dtype=np.float32)
    prob = prob.reshape(prob.shape[-2], prob.shape[-1])
    out = []
    comps = sorted(_label(prob > 0.3), key=lambda c: -c[4])[:200]
    sx, sy = w / prob.shape[1], h / prob.shape[0]
    for x0, y0, x1, y1, area, idx in comps:
        bw, bh = x1 - x0, y1 - y0
        if min(bw, bh) < 3:
            continue
        score = float(prob[idx].mean())
        if score < 0.5:
            continue
        d = bw * bh * 2.0 / (2.0 * (bw + bh))  # unclip ratio 2.0 on the box
        bx0, by0, bx1, by1 = x0 - d, y0 - d, x1 + d, y1 + d
        if min(bx1 - bx0, by1 - by0) < 5:
            continue
        out.append([int(max(0, bx0) * sx), int(max(0, by0) * sy), int(min(prob.shape[1], bx1) * sx),
                    int(min(prob.shape[0], by1) * sy), round(score, 3)])
    return out


def _nms(boxes: list[list], thr: float) -> list[list]:
    boxes = sorted(boxes, key=lambda b: -b[4])
    keep: list[list] = []
    for b in boxes:
        ok = True
        for k in keep:
            ix = max(0.0, min(b[2], k[2]) - max(b[0], k[0]))
            iy = max(0.0, min(b[3], k[3]) - max(b[1], k[1]))
            inter = ix * iy
            union = (b[2] - b[0]) * (b[3] - b[1]) + (k[2] - k[0]) * (k[3] - k[1]) - inter
            if union > 0 and inter / union > thr:
                ok = False
                break
        if ok:
            keep.append(b)
    return keep


def face_boxes(img: Image.Image, score_thr: float = 0.7) -> list[list]:
    """YuNet (2023mar) on the picture letterboxed into its 640 x 640 input."""
    sess = _session(FACE_MODEL)
    if sess is None:
        return []
    w, h = img.size
    s = 640.0 / max(w, h)
    rw, rh = max(1, int(round(w * s))), max(1, int(round(h * s)))
    canvas = np.zeros((640, 640, 3), np.float32)
    canvas[:rh, :rw] = np.asarray(img.convert("RGB").resize((rw, rh), Image.BILINEAR), dtype=np.float32)[..., ::-1]
    x = canvas.transpose(2, 0, 1)[None]
    names = [o.name for o in sess.get_outputs()]
    res = dict(zip(names, sess.run(None, {sess.get_inputs()[0].name: x})))
    faces = []
    for stride in (8, 16, 32):
        cols = 640 // stride
        cls = np.clip(res[f"cls_{stride}"].reshape(-1), 0, 1)
        obj = np.clip(res[f"obj_{stride}"].reshape(-1), 0, 1)
        bbox = res[f"bbox_{stride}"].reshape(-1, 4)
        score = np.sqrt(cls * obj)
        for i in np.flatnonzero(score >= score_thr).tolist():
            r, c = divmod(i, cols)
            cx, cy = (c + bbox[i, 0]) * stride, (r + bbox[i, 1]) * stride
            bw, bh = np.exp(bbox[i, 2]) * stride, np.exp(bbox[i, 3]) * stride
            faces.append([(cx - bw / 2) / s, (cy - bh / 2) / s, (cx + bw / 2) / s, (cy + bh / 2) / s, float(score[i])])
    return [[int(b[0]), int(b[1]), int(b[2]), int(b[3]), round(b[4], 3)] for b in _nms(faces, 0.3)]


def _morph(mask: np.ndarray, size: int, op) -> np.ndarray:
    out = mask.copy()
    border = op is np.minimum  # erosion never eats in from the border, dilation never grows from it
    p = np.pad(mask, ((0, size - 1), (0, size - 1)), constant_values=border)
    for dy in range(size):
        for dx in range(size):
            out = op(out, p[dy:dy + mask.shape[0], dx:dx + mask.shape[1]])
    return out


def diff_boxes(img: Image.Image, cleans: list[Image.Image]) -> list[list]:
    """Title area from a titled backdrop and a textless version of the same picture (if one matches)."""
    if not cleans:
        return []
    w, h = img.size
    a = np.asarray(img.convert("L").resize((192, 108), Image.BOX), dtype=np.int16)
    best = None
    for c in cleans:
        if abs(c.size[0] / c.size[1] - w / h) > 0.05:
            continue
        b = np.asarray(c.convert("L").resize((192, 108), Image.BOX), dtype=np.int16)
        d = np.abs(a - b)
        same = float((d < 18).mean())
        if same >= 0.75 and (best is None or same > best[0]):
            best = (same, d)
    if best is None:
        return []
    mask = best[1] >= 30
    mask = _morph(_morph(mask, 2, np.minimum), 2, np.maximum)  # opening 2 x 2
    mask = _morph(mask, 3, np.maximum)  # dilate 3 x 3
    sx, sy = w / 192.0, h / 108.0
    return [[round(x0 * sx), round(y0 * sy), round(x1 * sx), round(y1 * sy)]
            for x0, y0, x1, y1, area, _ in _label(mask) if area >= 12]


def analyze(img: Image.Image, cleans: list[Image.Image] | None = None) -> dict:
    w, h = img.size
    return dict(w=w, h=h, text=text_boxes(img), faces=face_boxes(img), diff=diff_boxes(img, list(cleans or [])),
                sal=saliency(img.convert("L")).tolist())


# ---------------------------------------------------------------------------------------------- placement
def _keep(a: dict, use_faces: bool = True, margin: float = MARGIN) -> list[tuple]:
    w, h = a["w"], a["h"]
    mx, my = margin * w, margin * h
    boxes = []
    faces = sorted(a["faces"] if use_faces else [], key=lambda b: -(b[3] - b[1]))
    if faces:  # main faces only: a crowd must not force the picture smaller
        big = faces[0][3] - faces[0][1]
        faces = [b for b in faces[:5] if (b[3] - b[1]) >= 0.5 * big]
    for kind, lst, minh in (("text", a["text"], TEXT_MIN_H), ("diff", a["diff"], TEXT_MIN_H), ("face", faces, FACE_MIN_H)):
        for b in lst:
            if (b[3] - b[1]) >= minh * h:
                boxes.append((kind, max(0, b[0] - mx), max(0, b[1] - my), min(w, b[2] + mx), min(h, b[3] + my)))
    return boxes


def _panel_left(y: float) -> float:
    """The window outline's left edge at height y (template px): rounded top corners (radius about 22), sides that
    taper inwards from x 0 at y 144 to x 14 at y 426, small rounded bottom corners."""
    r = 22.0
    corner = r - np.sqrt(max(0.0, r * r - (112.0 + r - y) ** 2)) if y < 112.0 + r else 0.0
    taper = 14.0 * (y - 144.0) / 282.0 if y > 144.0 else 0.0
    rb = 12.0
    bottom = (rb - np.sqrt(max(0.0, rb * rb - (y - (430.0 - rb)) ** 2))) if y > 430.0 - rb else 0.0
    return max(corner, taper + bottom)


def _x_interval(boxes, s, oy, pw, edge=EDGE_M):
    lo, hi = -1e9, 1e9
    for b in boxes:
        y0, y1 = oy + s * b[2], oy + s * b[4]
        if y0 < 112.0 + edge or y1 > 430.0 - edge:
            return None
        lm = max(_panel_left(y0), _panel_left(y1)) + edge
        lo = max(lo, lm - s * b[1])
        hi = min(hi, (pw - lm) - s * b[3])
    return (lo, hi) if lo <= hi else None


def _axis(lo_c, hi_c, size, pan, sal_profile):
    ideal = (min(0.0, pan - size), max(0.0, pan - size))
    base = max(0.0, pan - size)
    if lo_c > hi_c:
        return None
    lo, hi = max(lo_c, ideal[0]), min(hi_c, ideal[1])
    if lo <= hi:
        if hi - lo < 0.5 or sal_profile is None:
            return ((lo + hi) / 2.0, base)
        best, bo = -1.0, (lo + hi) / 2.0
        for t in np.linspace(lo, hi, 9):
            vis0, vis1 = (-t) / size, (pan - t) / size
            k0, k1 = int(max(0.0, vis0) * len(sal_profile)), int(min(1.0, vis1) * len(sal_profile))
            score = float(np.sum(sal_profile[k0:max(k1, k0 + 1)])) - 0.02 * abs(t - (lo + hi) / 2.0)
            if score > best:
                best, bo = score, float(t)
        return (bo, base)
    o = lo_c if lo_c > ideal[1] else hi_c
    dist = (lo_c - ideal[1]) if lo_c > ideal[1] else (ideal[0] - hi_c)
    return (o, base + dist)


def _solve(a, keep, ks, panel, edge, colp):
    w, h = a["w"], a["h"]
    pw, ph = panel[2] - panel[0], panel[3] - panel[1]
    s_cover = max(pw / w, ph / h)
    best = None
    for k in ks:
        s = s_cover * k
        W, H = w * s, h * s
        yb = (min(0.0, ph - H), max(0.0, ph - H))
        ys = np.arange(np.floor(min(yb[0], 0) - 30), np.ceil(max(yb[1], 0) + 30) + 1, 1.0)
        for oyr in ys:
            oy = panel[1] + oyr
            iv = _x_interval(keep, s, oy, pw, edge) if keep else (-1e9, 1e9)
            if iv is None:
                continue
            vs = max(0.0, ph - H) + max(0.0, yb[0] - oyr, oyr - yb[1])
            rx = _axis(iv[0], iv[1], W, pw, colp)
            if rx is None:
                continue
            area = rx[1] * ph + vs * pw
            centre = (ph - H) / 2.0
            if (best is None or area < best[0] - 1e-6 or (abs(area - best[0]) < 1e-6 and k > best[4] + 1e-9)
                    or (abs(area - best[0]) < 1e-6 and abs(k - best[4]) < 1e-9 and abs(oyr - centre) < abs(best[3] - centre))):
                best = (area, s, rx, oyr, k, vs)
        if best is not None and best[0] == 0:
            break
    return best


def place(a: dict, art_type: str = "keyart", panel=PANEL) -> dict:
    """Scale and offset for the picture, in this order:
      1. full cover with text, title-difference and main-face boxes inside the window outline
      2. full cover with only text and title boxes inside (faces may be cut at the edge)
      3. full cover with text boxes inside but with tighter margins
      4. only if text would otherwise be cut: the largest picture keeping the text inside, with the thin
         uncovered slivers filled from the picture's own edge (never for video frames and stills)
      5. otherwise centred cover, and the boxes that are cut are listed."""
    w, h = a["w"], a["h"]
    pw, ph = panel[2] - panel[0], panel[3] - panel[1]
    sal = np.array(a["sal"]) if a.get("sal") else None
    colp = sal.sum(axis=0) if sal is not None else None
    s_cover = max(pw / w, ph / h)

    def result(best, how, faces, keep):
        area, s, rx, oyr, k, vs = best
        ox, oy = panel[0] + rx[0], panel[1] + oyr
        box = (ox, oy, ox + w * s, oy + h * s)
        return dict(box=[round(float(v), 2) for v in box], scale=round(float(k), 3), sliver=round(float(max(rx[1], vs)), 1),
                    method="cover" if area == 0 else "fill", tier=how, faces_kept=faces, keep=len(keep), cut=[])

    tiers = [("faces+text", True, MARGIN, EDGE_M), ("text", False, MARGIN, EDGE_M), ("text-tight", False, 0.003, 1.5)]
    for how, faces, margin, edge in tiers:
        keep = _keep(a, faces, margin)
        best = _solve(a, keep, [1.0], panel, edge, colp)
        if best is not None and best[0] == 0:
            return result(best, how, faces, keep)
    keep = _keep(a, False, 0.003)
    if art_type not in ("frame", "still"):
        ks = list(np.arange(0.99, 0.795, -0.01)) + list(np.arange(0.78, 0.595, -0.02))
        best = _solve(a, keep, ks, panel, 1.5, colp)
        if best:
            r = result(best, "fill", False, keep)
            if r["method"] == "fill" and r["sliver"] < 1.0:  # under a pixel: grow slightly instead of filling
                b = r["box"]
                r["box"] = [b[0] - 1.5, b[1] - 1.5, b[2] + 1.5, b[3] + 1.5]
                r["method"] = "cover"
            return r
    s = s_cover
    best = _solve(a, [], [1.0], panel, 1.5, colp)
    ox, oy = panel[0] + best[2][0], panel[1] + best[3]
    cut = []
    for b in keep:
        iv = _x_interval([b], s, oy, pw, 1.5)
        if iv is None or not (iv[0] <= ox - panel[0] <= iv[1]):
            cut.append([b[0]] + [round(float(v)) for v in b[1:]])
    return dict(box=[round(float(v), 2) for v in (ox, oy, ox + w * s, oy + h * s)], scale=1.0, sliver=0.0,
                method="cover-cut" if cut else "cover", tier="cover", faces_kept=False, keep=len(keep), cut=cut)


def crop_bars(img: Image.Image, limit: int = 18) -> Image.Image:
    """A video frame without its black bars (rows and columns darker than limit at the edges)."""
    g = np.asarray(img.convert("L").filter(ImageFilter.BoxBlur(1)), dtype=np.float32)
    rows = np.flatnonzero(g.mean(axis=1) > limit)
    cols = np.flatnonzero(g.mean(axis=0) > limit)
    if len(rows) < 10 or len(cols) < 10:
        return img
    return img.crop((int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1))
