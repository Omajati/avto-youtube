"""One take for product ads: a virtual camera glides over ONE high-resolution picture.

The product is drawn once and never redrawn or animated, so it cannot jump, morph or melt.
Each scene is a stretch of one continuous camera path (push in, slide over a detail, pull back to the hero);
a few stretches start with a clean cut to another framing of the same set, like a real product spot.
"""
import subprocess
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

# Where the camera goes for n scenes: (from framing, to framing). A stretch whose start differs from the end
# of the previous one opens with a cut. "~" = the same framing a little wider and higher, for a slow drift.
PATHS = {
    1: [("hero~", "hero")],
    2: [("wide", "push"), ("detail", "hero")],
    3: [("wide", "push"), ("detail2~", "detail2"), ("detail", "hero")],
    4: [("wide", "push"), ("push", "detail"), ("detail2~", "detail2"), ("detail~", "hero")],
    5: [("wide", "push"), ("push", "detail"), ("detail2~", "detail2"), ("base~", "base"), ("detail~", "hero")],
    6: [("wide", "push"), ("push", "detail"), ("detail", "detail2"), ("base~", "base"), ("wide~", "push"),
        ("detail~", "hero")],
}
# The words shown in the scene list for each framing.
NAMES = {"wide": "общий план", "push": "наезд на товар", "detail": "макро главной детали",
         "detail2": "макро второй детали", "base": "низ товара и подиум", "hero": "отъезд на товар целиком"}


def orbit_scenes(n: int, mode: str) -> list:
    """Scenes made as a Kling orbit: one in the middle (the product seen whole after the push-in), or all."""
    if mode == "all":
        return list(range(n))
    if mode == "one":
        return [1 if n >= 2 else 0]
    return []


def path_for(n: int) -> list:
    if n in PATHS:
        return PATHS[n]
    middle = PATHS[6][1:-1]
    return [PATHS[6][0]] + [middle[i % len(middle)] for i in range(n - 2)] + [PATHS[6][-1]]


def _clamp(box, W, H):
    x0, y0, x1, y1 = [float(v) for v in box]
    x0, x1 = sorted((max(0.0, min(1.0, x0)), max(0.0, min(1.0, x1))))
    y0, y1 = sorted((max(0.0, min(1.0, y0)), max(0.0, min(1.0, y1))))
    if x1 - x0 < 0.02 or y1 - y0 < 0.02:
        return None
    return x0 * W, y0 * H, x1 * W, y1 * H


def framings(W: int, H: int, aspect: float, product, detail=None, detail2=None, max_zoom: float = 2.6) -> dict:
    """Camera rectangles (cx, cy, height) in picture pixels. aspect = output width / height.
    product/detail boxes are fractions (x0, y0, x1, y1) of the picture."""
    full_h = min(H, W / aspect)
    min_h = full_h / max_zoom
    p = _clamp(product, W, H) or (W * 0.3, H * 0.25, W * 0.7, H * 0.8)
    pcx, pcy, pw, ph = (p[0] + p[2]) / 2, (p[1] + p[3]) / 2, p[2] - p[0], p[3] - p[1]

    def fit(cx, cy, h):
        h = max(min_h, min(full_h, h))
        w = h * aspect
        cx = min(max(cx, w / 2), W - w / 2)
        cy = min(max(cy, h / 2), H - h / 2)
        return cx, cy, h

    def around(box, pad):
        cx, cy, bw, bh = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, box[2] - box[0], box[3] - box[1]
        return fit(cx, cy, max(bh, bw / aspect) * pad)

    d1 = _clamp(detail, W, H) if detail else None
    d1 = d1 or (p[0], p[1], p[2], p[1] + ph * 0.3)  # no detail found: the top of the product
    d2 = _clamp(detail2, W, H) if detail2 else None
    d2 = d2 or (p[0], p[1] + ph * 0.35, p[2], p[1] + ph * 0.65)
    f = {
        "wide": fit(W / 2, H / 2, full_h),
        "push": fit(pcx, pcy, max(ph, pw / aspect) * 1.35),
        "hero": fit(pcx, pcy + ph * 0.02, max(ph, pw / aspect) * 1.12),
        "detail": around(d1, 1.6),
        "detail2": around(d2, 1.7),
        "base": fit(pcx, p[3] - ph * 0.12, max(ph * 0.55, pw * 1.6 / aspect)),
    }
    for k, (cx, cy, h) in list(f.items()):  # the slow drift starts a little wider and higher
        f[k + "~"] = fit(cx, cy - h * 0.05, h * 1.1)
    return f


def _ease(t: float) -> float:
    return t * t * (3 - 2 * t)  # smoothstep: starts and stops softly, like a slider or a gimbal


def _sweep(w: int, h: int, t: float) -> Image.Image:
    """A soft diagonal light glint crossing the frame (t from 0 to 1)."""
    band = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(band)
    x = -0.6 * w + t * 2.2 * w
    d.polygon([(x, h), (x + 0.18 * w, h), (x + 0.18 * w + 0.6 * w, 0), (x + 0.6 * w, 0)], fill=38)
    return band.filter(ImageFilter.GaussianBlur(w * 0.05))


def prepare(master: Path, out: Path, long_side: int = 3840) -> Path:
    """A big sharp copy of the picture so the macro framings stay crisp (when no AI upscale was made)."""
    im = Image.open(master).convert("RGB")
    k = long_side / max(im.size)
    if k > 1.05:
        im = im.resize((round(im.width * k), round(im.height * k)), Image.LANCZOS)
        im = im.filter(ImageFilter.UnsharpMask(radius=2, percent=60, threshold=2))
    im.save(out, quality=95)
    return out


def crop(big: Image.Image, rect: tuple, w: int, h: int) -> Image.Image:
    cx, cy, hh = rect
    ww = hh * w / h
    return big.transform((w, h), Image.EXTENT, (cx - ww / 2, cy - hh / 2, cx + ww / 2, cy + hh / 2), Image.BICUBIC)


def render_scene(big: Image.Image, a: tuple, b: tuple, frames: int, out: Path, w: int, h: int, fps: int,
                 fade_in: bool = False, fade_out: bool = False, glint: bool = False):
    """One stretch of the camera path from rectangle a to rectangle b, written as an mp4."""
    aspect = w / h
    from .media import ffmpeg
    cmd = [ffmpeg(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
           "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "medium", "-crf", "17",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    fade = max(1, round(fps * 0.4))
    try:
        for i in range(frames):
            t = _ease(i / max(1, frames - 1))
            cx, cy, hh = (a[k] + (b[k] - a[k]) * t for k in range(3))
            ww = hh * aspect
            fr = big.transform((w, h), Image.EXTENT, (cx - ww / 2, cy - hh / 2, cx + ww / 2, cy + hh / 2),
                               Image.BICUBIC)
            if glint:
                g = (i / max(1, frames - 1) - 0.35) / 0.5  # the glint passes in the middle of the stretch
                if 0 <= g <= 1:
                    fr = ImageChops.add(fr, Image.merge("RGB", [_sweep(w, h, g)] * 3))
            k = 1.0
            if fade_in and i < fade:
                k = i / fade
            if fade_out and i >= frames - fade:
                k = min(k, (frames - 1 - i) / fade)
            if k < 1.0:
                fr = Image.eval(fr, lambda v, k=k: int(v * k))
            p.stdin.write(fr.tobytes())
    finally:
        p.stdin.close()
        p.wait()
    if p.returncode != 0:
        raise RuntimeError("ffmpeg не собрал кусок съёмки одним дублем")
