"""Call to action on Shorts: animated YouTube-style "Subscribe" and "Like" buttons, pressed by a cursor, in the
second half of the video (above the subtitles, below YouTube's own top buttons)."""
import math
import shutil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import captions, media

FPS = 30
W, H = 900, 330          # the overlay canvas, placed centred on a 1080-wide frame
SS = 2                   # supersampling for smooth edges
RED, BLUE = (255, 0, 0, 255), (62, 166, 255, 255)
TEXT = {"ru": ("ПОДПИСАТЬСЯ", "ВЫ ПОДПИСАНЫ", "НРАВИТСЯ"), "en": ("SUBSCRIBE", "SUBSCRIBED", "LIKE"),
        "de": ("ABONNIEREN", "ABONNIERT", "GEFÄLLT MIR"), "es": ("SUSCRIBIRSE", "SUSCRITO", "ME GUSTA"),
        "fr": ("S'ABONNER", "ABONNÉ", "J'AIME")}
# Pill widths (subscribe, like) so the longer words fit.
WIDTH = {"ru": (560, 480), "en": (500, 380), "de": (540, 520), "es": (560, 420), "fr": (500, 380)}
LANGS = {"auto": "Как язык ролика", "ru": "Русский: «Подписаться», «Нравится»", "en": "Английский: Subscribe, Like",
         "de": "Немецкий: Abonnieren, Gefällt mir", "es": "Испанский: Suscribirse, Me gusta",
         "fr": "Французский: S'abonner, J'aime"}
MODES = {"both": "Подписка и лайк", "subscribe": "Только «Подписаться»", "like": "Только «Нравится»", "off": "Выключено"}


def _font(size: int):
    for p in captions.FONT_CANDIDATES:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _ease_back(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    c = 1.70158
    return 1 + (c + 1) * (x - 1) ** 3 + c * (x - 1) ** 2


def _ease(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    return 1 - (1 - x) ** 3


def _cursor(scale: float) -> Image.Image:
    pts = [(0, 0), (0, 52), (13, 40), (22, 60), (31, 56), (22, 37), (40, 37)]
    k = 1.7 * scale * SS
    im = Image.new("RGBA", (int(80 * SS * scale) + 8, int(110 * SS * scale) + 8), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.polygon([(4 + x * k, 4 + y * k) for x, y in pts], fill=(255, 255, 255, 255), outline=(0, 0, 0, 255),
              width=max(2, int(3 * SS * scale)))
    return im


def _bell(size: int, color) -> Image.Image:
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    s = size
    d.ellipse([s * .42, s * .04, s * .58, s * .2], fill=color)
    d.pieslice([s * .18, s * .12, s * .82, s * .76], 180, 360, fill=color)
    d.rectangle([s * .18, s * .44, s * .82, s * .7], fill=color)
    d.polygon([(s * .18, s * .66), (s * .82, s * .66), (s * .94, s * .8), (s * .06, s * .8)], fill=color)
    d.ellipse([s * .4, s * .8, s * .6, s * .96], fill=color)
    return im


def _thumb(size: int, fill, outline) -> Image.Image:
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    s = size
    w = max(2, int(s * .07))
    d.rounded_rectangle([s * .06, s * .44, s * .26, s * .92], radius=s * .04, fill=fill, outline=outline, width=w)
    d.polygon([(s * .32, s * .46), (s * .5, s * .08), (s * .6, s * .1), (s * .6, s * .38), (s * .9, s * .38),
               (s * .96, s * .48), (s * .86, s * .92), (s * .32, s * .92)], fill=fill, outline=outline, width=w)
    return im


def _place(canvas: Image.Image, layer: Image.Image, scale: float, alpha: float, cx: float, cy: float):
    if scale <= 0.01 or alpha <= 0.01:
        return
    lw, lh = max(1, int(layer.width * scale)), max(1, int(layer.height * scale))
    im = layer.resize((lw, lh), Image.LANCZOS)
    if alpha < 1:
        a = im.getchannel("A").point(lambda v: int(v * alpha))
        im.putalpha(a)
    canvas.alpha_composite(im, (int(cx - lw / 2), int(cy - lh / 2)))


def _pill(w: int, h: int, color, label: str, icon: Image.Image = None, text_color=(255, 255, 255, 255)) -> Image.Image:
    """A rounded button at least w wide; wider when the text needs it."""
    f = _font(int(h * .42) * SS)
    tw = ImageDraw.Draw(Image.new("RGBA", (1, 1))).textlength(label, font=f)
    iw = icon.width + 18 * SS if icon else 0
    w = max(w, int((tw + iw) / SS) + 110)
    im = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, w * SS - 1, h * SS - 1], radius=h * SS // 2, fill=color)
    x = (w * SS - tw - iw) / 2
    if icon:
        im.alpha_composite(icon, (int(x), int((h * SS - icon.height) / 2)))
        x += iw
    d.text((x, h * SS / 2), label, font=f, fill=text_color, anchor="lm")
    return im


def subscribe_frame(t: float, lang: str) -> Image.Image:
    sub, done, _ = TEXT.get(lang, TEXT["en"])
    canvas = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    cx, cy = W * SS / 2, H * SS / 2 - 20 * SS
    click = 1.25
    if t < click:
        btn = _pill(WIDTH.get(lang, WIDTH["en"])[0], 130, RED, sub)
    else:
        bell = _bell(56 * SS, (15, 15, 15, 255))
        ang = 18 * math.sin((t - click) * 22) * max(0.0, 1 - (t - click) / 0.8)
        bell = bell.rotate(ang, resample=Image.BICUBIC, center=(bell.width / 2, 6 * SS))
        btn = _pill(WIDTH.get(lang, WIDTH["en"])[0], 130, (242, 242, 242, 255), done, bell, (15, 15, 15, 255))
    scale = 0.3 + 0.7 * _ease_back(t / 0.35)
    if click <= t < click + 0.15:
        scale *= 0.93
    if t > 2.6:
        scale *= 1 - 0.4 * _ease((t - 2.6) / 0.4)
    alpha = min(1.0, t / 0.2) * (1 - _ease((t - 2.6) / 0.4) if t > 2.6 else 1)
    _place(canvas, btn, scale, alpha, cx, cy)
    if click <= t < click + 0.45:  # ripple
        r = (t - click) / 0.45
        d = ImageDraw.Draw(canvas)
        rad = (30 + 90 * r) * SS
        d.ellipse([cx + 150 * SS - rad, cy + 10 * SS - rad, cx + 150 * SS + rad, cy + 10 * SS + rad],
                  outline=(255, 255, 255, int(200 * (1 - r))), width=6 * SS)
    if 0.55 < t < 2.5:  # cursor flies in and clicks
        k = _ease((t - 0.55) / 0.6)
        x = cx + 150 * SS + (1 - k) * 260 * SS
        y = cy + 10 * SS + (1 - k) * 160 * SS
        cur = _cursor(0.85 if click <= t < click + 0.15 else 1.0)
        ca = min(1.0, (t - 0.55) / 0.15) * (1 - max(0.0, (t - 2.2) / 0.3))
        _place(canvas, cur, 1.0, ca, x + cur.width / 2 - 4, y + cur.height / 2 - 4)
    return canvas.resize((W, H), Image.LANCZOS)


def like_frame(t: float, lang: str) -> Image.Image:
    label = TEXT.get(lang, TEXT["en"])[2]
    canvas = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    cx, cy = W * SS / 2, H * SS / 2 - 20 * SS
    click = 1.0
    liked = t >= click
    icon = _thumb(78 * SS, BLUE if liked else (0, 0, 0, 0), BLUE if liked else (255, 255, 255, 255))
    if liked:  # the thumb pops
        p = min(1.0, (t - click) / 0.35)
        k = 1 + 0.35 * math.sin(p * math.pi)
        icon = icon.resize((int(icon.width * k), int(icon.height * k)), Image.LANCZOS)
    btn = _pill(WIDTH.get(lang, WIDTH["en"])[1], 130, (33, 33, 33, 235), label, icon)
    scale = 0.3 + 0.7 * _ease_back(t / 0.35)
    if click <= t < click + 0.15:
        scale *= 0.93
    if t > 2.2:
        scale *= 1 - 0.4 * _ease((t - 2.2) / 0.4)
    alpha = min(1.0, t / 0.2) * (1 - _ease((t - 2.2) / 0.4) if t > 2.2 else 1)
    _place(canvas, btn, scale, alpha, cx, cy)
    if click <= t < click + 0.6:  # a burst of dots around the thumb
        r = (t - click) / 0.6
        d = ImageDraw.Draw(canvas)
        bx = cx - (btn.width * scale) / 2 + 90 * SS
        for i in range(10):
            a = i * math.pi / 5
            dist = (50 + 120 * _ease(r)) * SS
            rr = 9 * SS * (1 - r)
            px, py = bx + math.cos(a) * dist, cy + math.sin(a) * dist
            d.ellipse([px - rr, py - rr, px + rr, py + rr], fill=(62, 166, 255, int(255 * (1 - r))))
    if 0.35 < t < 2.1:
        k = _ease((t - 0.35) / 0.55)
        x = cx - (btn.width * scale) / 2 + 90 * SS + (1 - k) * 300 * SS
        y = cy + (1 - k) * 170 * SS
        cur = _cursor(0.85 if click <= t < click + 0.15 else 1.0)
        ca = min(1.0, (t - 0.35) / 0.15) * (1 - max(0.0, (t - 1.8) / 0.3))
        _place(canvas, cur, 1.0, ca, x + cur.width / 2 - 4, y + cur.height / 2 - 4)
    return canvas.resize((W, H), Image.LANCZOS)


DURATION = {"subscribe": 3.0, "like": 2.6}


def _frames(kind: str, lang: str, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    draw = subscribe_frame if kind == "subscribe" else like_frame
    n = int(DURATION[kind] * FPS)
    for i in range(n):
        draw(i / FPS, lang).save(folder / f"f{i:03d}.png")
    return folder / "f%03d.png"


def plan(mode: str, total: float) -> list:
    """[(kind, start seconds)] in the second half of the video."""
    if mode not in ("both", "subscribe", "like") or total < 8:
        return []
    kinds = ["subscribe", "like"] if mode == "both" else [mode]
    if len(kinds) == 2 and total < 16:
        kinds = ["subscribe"]  # one button is enough on a very short Short
    if len(kinds) == 1:
        return [(kinds[0], round(min(total * 0.55, total - DURATION[kinds[0]] - 1.5), 2))]
    return [("subscribe", round(total * 0.52, 2)), ("like", round(min(total * 0.75, total - DURATION["like"] - 1.5), 2))]


def overlay(video: Path, out: Path, mode: str, lang: str, total: float, width: int, height: int, work: Path) -> bool:
    """Draws the buttons over a silent picture-only video. Returns False when nothing was added."""
    items = plan(mode, total)
    if not items:
        return False
    lang = lang if lang in TEXT else "en"
    k = width / 1080
    ow, oh = int(W * k) // 2 * 2, int(H * k) // 2 * 2
    x, y = (width - ow) // 2, int(height * 0.16)
    args, fc, last = ["-i", video], [], "0:v"
    for i, (kind, start) in enumerate(items):
        args += ["-framerate", FPS, "-i", _frames(kind, lang, work / f"cta_{kind}")]
        fc.append(f"[{i + 1}:v]scale={ow}:{oh},format=rgba,setpts=PTS-STARTPTS+{start}/TB[c{i}]")
        fc.append(f"[{last}][c{i}]overlay={x}:{y}:eof_action=pass:format=auto[o{i}]")
        last = f"o{i}"
    try:
        media.run([*args, "-filter_complex", ";".join(fc), "-map", f"[{last}]", "-c:v", "libx264", "-preset", "veryfast",
                   "-crf", 18, "-pix_fmt", "yuv420p", "-an", out], "Призыв подписаться")
    finally:
        for kind, _ in items:
            shutil.rmtree(work / f"cta_{kind}", ignore_errors=True)
    return True
