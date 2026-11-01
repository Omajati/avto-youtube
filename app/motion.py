"""Motion design scenes: a map or a timeline drawn frame by frame in place of a picture, where the script talks about
places, routes or dates. A map shows the region from afar, flies in, draws the borders of the countries in the story,
fills them, draws a route and drops pins with names. A timeline draws its line, and the dates pop in one by one up to
the moment the narration is about. Everything is drawn here (no paid service) and encoded like any other scene clip."""
import json
import math
import re
import subprocess
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from . import media
from .media import UserError

GEO = Path(__file__).parent / "geo" / "countries.json"
KINDS = {"map": "🗺 Карта", "timeline": "📅 Линия времени", "chart": "📈 График", "compare": "⚖️ Сравнение",
         "quote": "💬 Цитата", "list": "📋 Список"}
MODES = {"auto": "Авто: карты и даты там, где они к месту", "off": "Выключено"}

OCEAN_TOP, OCEAN_BOTTOM = (12, 22, 40), (6, 12, 24)
LAND, LAND_EDGE, BORDER = (52, 60, 74), (92, 104, 124), (78, 88, 106)
GOLD, GOLD_DEEP, RED, WHITE = (250, 204, 21), (212, 160, 23), (239, 68, 68), (255, 255, 255)
BLUE, MUTED = (96, 165, 250), (150, 160, 180)
BG_TOP, BG_BOTTOM = (18, 24, 38), (6, 8, 14)

BOLD = ["/System/Library/Fonts/Supplemental/Arial Black.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"]
PLAIN = ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"]


@lru_cache(maxsize=64)
def _font(size: int, heavy: bool = False):
    for p in (BOLD if heavy else PLAIN):
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _ease(x: float) -> float:  # ease in-out
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


def _out(x: float) -> float:  # ease out
    x = min(max(x, 0.0), 1.0)
    return 1 - (1 - x) ** 3


def _back(x: float) -> float:  # a small overshoot, for things that pop in
    if x <= 0:
        return 0.0
    x = min(x, 1.0)
    c = 1.70158
    return 1 + (c + 1) * (x - 1) ** 3 + c * (x - 1) ** 2


def _phase(t: float, a: float, b: float) -> float:
    return min(max((t - a) / (b - a), 0.0), 1.0) if b > a else float(t >= a)


def _text(d: ImageDraw.ImageDraw, xy, s: str, font, fill=WHITE, anchor="mm", alpha: float = 1.0, shadow: int = 3):
    if alpha <= 0.01 or not s:
        return
    a = int(255 * alpha)
    if shadow:
        d.text((xy[0] + shadow, xy[1] + shadow), s, font=font, fill=(0, 0, 0, int(a * 0.7)), anchor=anchor)
    d.text(xy, s, font=font, fill=(*fill[:3], a), anchor=anchor)


class _Labels:
    """Places names next to their points so they never cover each other: above, below, right or left."""

    def __init__(self):
        self.taken = []

    def place(self, x, y, s, font, gap=26, must=True):
        tw, th = font.getlength(s), font.size
        for dx, dy in ((0, -gap - th / 2), (0, gap + th / 2), (gap + tw / 2, 0), (-gap - tw / 2, 0)):
            cx, cy = x + dx, y + dy
            box = (cx - tw / 2 - 6, cy - th / 2 - 4, cx + tw / 2 + 6, cy + th / 2 + 4)
            if not any(box[0] < b[2] and b[0] < box[2] and box[1] < b[3] and b[1] < box[3] for b in self.taken):
                self.taken.append(box)
                return cx, cy
        if must:
            return x, y - gap - th / 2
        return None

    def block(self, x, y, r):
        self.taken.append((x - r, y - r, x + r, y + r))


def _wrap(s: str, font, width: int, lines: int = 2) -> list:
    words, out, cur = s.split(), [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if font.getlength(test) <= width or not cur:
            cur = test
        else:
            out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    if len(out) > lines:
        out = out[:lines]
        out[-1] = out[-1].rstrip(",.;:") + "…"
    return out


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    img = img.convert("RGB")
    k = max(w / img.width, h / img.height)
    img = img.resize((max(w, int(img.width * k + 0.5)), max(h, int(img.height * k + 0.5))), Image.LANCZOS)
    x, y = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((x, y, x + w, y + h))


def _gradient(w: int, h: int, top, bottom) -> Image.Image:
    col = Image.new("RGB", (1, h))
    for y in range(h):
        k = y / max(1, h - 1)
        col.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * k) for i in range(3)))
    return col.resize((w, h))


def _vignette(w: int, h: int, strength: float = 0.55) -> Image.Image:
    """A dark edge as a multiply layer (white in the middle)."""
    small = Image.new("L", (64, 36), 0)
    d = ImageDraw.Draw(small)
    for k in range(18):
        v = int(255 * (1 - strength * (1 - k / 18) ** 1.6))
        d.ellipse([-8 + k * 1.6, -6 + k * 1.0, 72 - k * 1.6, 42 - k * 1.0], fill=v)
    return small.resize((w, h), Image.BICUBIC).filter(ImageFilter.GaussianBlur(w / 60)).convert("RGB")


class Encoder:
    """Frames straight into FFmpeg, no files in between."""

    def __init__(self, out: Path, w: int, h: int, fps: int, intro: Image.Image = None):
        """intro: the picture of the scene before; the motion grows out of it in the first half second."""
        self.out, self.tmp = out, out.with_suffix(".motion.mp4")
        self.w, self.h, self.k, self.n_intro = w, h, 0, int(fps * 0.5) if intro is not None else 0
        self.intro = _cover(intro, int(w * 1.1), int(h * 1.1)) if intro is not None else None
        self.p = subprocess.Popen([media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
                                   "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
                                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                                   "-r", str(fps), str(self.tmp)],
                                  stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    def add(self, frame: Image.Image):
        frame = frame.convert("RGB")
        if self.k < self.n_intro:  # the old picture pushes on and dissolves into the new scene
            f = self.k / self.n_intro
            z = 1.0 + 0.1 * _out(f)
            cw, ch = self.intro.width / 1.1 / z * 1.0, self.intro.height / 1.1 / z
            cx, cy = self.intro.width / 2, self.intro.height / 2
            old = self.intro.resize((self.w, self.h), Image.BILINEAR, box=(cx - cw / 2, cy - ch / 2, cx + cw / 2, cy + ch / 2))
            frame = Image.blend(old, frame, _ease(f))
        self.k += 1
        self.p.stdin.write(frame.tobytes())

    def close(self):
        self.p.stdin.close()
        err = self.p.stderr.read().decode("utf-8", "replace")
        if self.p.wait() != 0:
            self.tmp.unlink(missing_ok=True)
            raise UserError(f"Моушен-сцена не собралась: {err[-300:]}")
        self.tmp.replace(self.out)

    def abort(self):
        try:
            self.p.kill()
        finally:
            self.tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------- countries

@lru_cache(maxsize=1)
def countries() -> list:
    return json.loads(GEO.read_text("utf-8"))


def _norm(s: str) -> str:
    return re.sub(r"[^\w]", "", (s or "").lower().replace("ё", "е"))


ALIASES = {"usa": "USA", "сша": "USA", "america": "USA", "unitedstates": "USA", "uk": "GBR", "britain": "GBR",
           "великобритания": "GBR", "англия": "GBR", "england": "GBR", "россия": "RUS", "russia": "RUS",
           "ссср": "RUS", "ussr": "RUS", "китай": "CHN", "china": "CHN", "абиссиния": "ETH", "abyssinia": "ETH",
           "персия": "IRN", "persia": "IRN", "бирма": "MMR", "burma": "MMR", "занзибар": "TZA", "zanzibar": "TZA",
           "конго": "COD", "congo": "COD", "корея": "KOR", "korea": "KOR", "чехия": "CZE", "czechia": "CZE"}


def find_country(name: str):
    n = _norm(name)
    if not n:
        return None
    code = ALIASES.get(n)
    for c in countries():
        if code and c["a3"] == code:
            return c
        if n.upper() in (c["a3"], c["iso"]) or n in {_norm(c.get(k) or "") for k in ("en", "ru", "de", "es", "fr")}:
            return c
    return None


def _ring_area(r: list) -> float:
    return abs(sum(r[i][0] * r[i - 1][1] - r[i - 1][0] * r[i][1] for i in range(len(r)))) / 2


def _main_rings(c: dict) -> list:
    """The rings of a country near its biggest one: far overseas islands do not stretch the view."""
    big = max(c["r"], key=_ring_area)
    bx = sum(p[0] for p in big) / len(big)
    by = sum(p[1] for p in big) / len(big)
    out = []
    for r in c["r"]:
        mx, my = sum(p[0] for p in r) / len(r), sum(p[1] for p in r) / len(r)
        if abs(mx - bx) < 25 and abs(my - by) < 20:
            out.append(r)
    return out or [big]


# ---------------------------------------------------------------- map

class _View:
    """Equirectangular projection around the view centre (longitudes squeezed by cos of the latitude)."""

    def __init__(self, lon0, lat0, w_deg, h_deg, px_w, px_h):
        self.lon0, self.lat0 = lon0, lat0
        self.k = math.cos(math.radians(max(-70, min(70, lat0))))
        self.w_deg, self.h_deg, self.px_w, self.px_h = w_deg, h_deg, px_w, px_h

    def lon(self, x):
        while x - self.lon0 > 180:
            x -= 360
        while x - self.lon0 < -180:
            x += 360
        return x

    def xy(self, lon, lat):
        return ((self.lon(lon) - self.lon0) * self.k / self.w_deg + 0.5) * self.px_w, \
               (0.5 - (lat - self.lat0) / self.h_deg) * self.px_h

    def ring(self, r):
        # The ring is moved as a whole to the side of the view it is closest to.
        mean = sum(p[0] for p in r) / len(r)
        shift = self.lon(mean) - mean
        return [((p[0] + shift - self.lon0) * self.k / self.w_deg * self.px_w + self.px_w / 2,
                 (0.5 - (p[1] - self.lat0) / self.h_deg) * self.px_h) for p in r]


def _points(spec: dict, key: str) -> list:
    out = []
    for p in spec.get(key) or []:
        try:
            lat, lon = float(p.get("lat")), float(p.get("lon"))
        except (TypeError, ValueError, AttributeError):
            continue
        if -85 <= lat <= 85 and -180 <= lon <= 180:
            out.append({"name": str(p.get("name") or "").strip()[:28], "lat": lat, "lon": lon})
    return out


def map_ok(spec: dict) -> bool:
    return bool([c for c in spec.get("countries") or [] if find_country(str(c))]) or len(_points(spec, "route")) >= 2 \
        or len(_points(spec, "pins")) >= 1


def _extent(spec: dict, w: int, h: int):
    """Centre and size (in projected degrees) of the final view."""
    found = [find_country(str(c)) for c in spec.get("countries") or []]
    found = [c for c in found if c]
    pts = [(p["lon"], p["lat"]) for p in _points(spec, "route") + _points(spec, "pins")]
    for c in found:
        for r in _main_rings(c):
            pts += [(p[0], p[1]) for p in r[::3]]
    if not pts:
        raise UserError("Для карты не нашлось ни одной страны или точки")
    lon0 = pts[0][0]
    lons = [lon0 + ((x - lon0 + 180) % 360 - 180) for x, _ in pts]
    lats = [y for _, y in pts]
    lat0 = (max(lats) + min(lats)) / 2
    k = math.cos(math.radians(max(-70, min(70, lat0))))
    cx = (max(lons) + min(lons)) / 2
    span_w = max((max(lons) - min(lons)) * k * 1.45, 7.0)
    span_h = max((max(lats) - min(lats)) * 1.45 + 2.0, 4.0)
    aspect = w / h
    if span_w / span_h < aspect:
        span_w = span_h * aspect
    else:
        span_h = span_w / aspect
    span_h = min(span_h, 150)
    return ((cx + 180) % 360) - 180, max(-60, min(70, lat0)), span_w, span_h, found


def render_map(spec: dict, out: Path, seconds: float, w: int, h: int, fps: int, lang: str = "ru",
               poster: Path = None, prev: Image.Image = None):
    lon0, lat0, ew, eh, found = _extent(spec, w, h)
    zoom = 2.3 if ew < 60 else 1.6  # the camera starts this much wider and flies in
    bw, bh = int(w * zoom), int(h * zoom)
    view = _View(lon0, lat0, ew * zoom, eh * zoom, bw, bh)
    ss = 2
    # The world once, big enough for the last frame: ocean, land, borders, the countries of the story.
    land = Image.new("L", (bw * ss, bh * ss), 0)
    edges = Image.new("L", (bw * ss, bh * ss), 0)
    fill = Image.new("L", (bw * ss, bh * ss), 0)
    dl, de, df = ImageDraw.Draw(land), ImageDraw.Draw(edges), ImageDraw.Draw(fill)
    hl = {c["a3"] for c in found}
    pad = max(view.w_deg, view.h_deg) * 0.8
    for c in countries():
        for r in c["r"]:
            mean = sum(p[0] for p in r) / len(r)
            if abs(view.lon(mean) - lon0) * view.k > pad + 40 or abs(sum(p[1] for p in r) / len(r) - lat0) > pad + 30:
                continue
            pts = [(x * ss, y * ss) for x, y in view.ring(r)]
            if len(pts) < 3:
                continue
            dl.polygon(pts, fill=255)
            de.line(pts + [pts[0]], fill=255, width=max(1, int(1.2 * ss * zoom)))
            if c["a3"] in hl:
                df.polygon(pts, fill=255)
    land = land.resize((bw, bh), Image.LANCZOS)
    edges = edges.resize((bw, bh), Image.LANCZOS)
    fill = fill.resize((bw, bh), Image.LANCZOS)
    base = _gradient(bw, bh, OCEAN_TOP, OCEAN_BOTTOM)
    glow = land.filter(ImageFilter.GaussianBlur(10 * zoom)).point(lambda v: int(v * 0.35))  # coastal haze
    base.paste(Image.new("RGB", (bw, bh), (22, 44, 72)), (0, 0), glow)
    base.paste(Image.new("RGB", (bw, bh), LAND), (0, 0), land)
    base.paste(Image.new("RGB", (bw, bh), BORDER), (0, 0), edges.point(lambda v: int(v * 0.8)))
    gd = ImageDraw.Draw(base)  # a faint graticule
    step = 5 if view.w_deg < 60 else 15
    for g in range(-180, 181, step):
        x, _ = view.xy(g, lat0)
        if 0 <= x <= bw:
            gd.line([(x, 0), (x, bh)], fill=(26, 38, 60), width=1)
    for g in range(-80, 81, step):
        _, y = view.xy(lon0, g)
        if 0 <= y <= bh:
            gd.line([(0, y), (bw, y)], fill=(26, 38, 60), width=1)
    base.paste(Image.new("RGB", (bw, bh), LAND), (0, 0), land.point(lambda v: 255 if v > 250 else 0))
    # A soft relief on the land, so it is not a flat cut-out.
    grain = Image.effect_noise((max(8, bw // 24), max(8, bh // 24)), 60).resize((bw, bh), Image.BICUBIC)
    grain = grain.filter(ImageFilter.GaussianBlur(3))
    lit = ImageChops.multiply(grain, land).point(lambda v: int(max(0, v - 90) * 0.5))
    shade = ImageChops.multiply(ImageChops.invert(grain), land).point(lambda v: int(max(0, v - 90) * 0.5))
    base.paste(Image.new("RGB", (bw, bh), (74, 84, 100)), (0, 0), lit)
    base.paste(Image.new("RGB", (bw, bh), (36, 42, 54)), (0, 0), shade)
    base.paste(Image.new("RGB", (bw, bh), BORDER), (0, 0), edges.point(lambda v: int(v * 0.8)))
    gold = Image.new("RGB", (w, h), GOLD_DEEP)

    # Outlines of the story's countries, as one long path each, for the drawing-on effect.
    outlines = []
    for c in found:
        for r in _main_rings(c):
            p = view.ring(r)
            ln = sum(math.dist(p[i], p[i - 1]) for i in range(1, len(p)))
            if ln > 8:
                outlines.append((p, ln))
    total_outline = sum(ln for _, ln in outlines) or 1
    route = [view.xy(p["lon"], p["lat"]) for p in _points(spec, "route")]
    route_names = [p["name"] for p in _points(spec, "route")]
    pins = [(view.xy(p["lon"], p["lat"]), p["name"]) for p in _points(spec, "pins")]
    curve = []
    for a, b in zip(route, route[1:]):  # each leg a soft arc
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        dx, dy = b[0] - a[0], b[1] - a[1]
        cx, cy = mx - dy * 0.18, my + dx * 0.18
        for k in range(24):
            s = k / 24
            curve.append(((1 - s) ** 2 * a[0] + 2 * (1 - s) * s * cx + s * s * b[0],
                          (1 - s) ** 2 * a[1] + 2 * (1 - s) * s * cy + s * s * b[1]))
    if route:
        curve.append(route[-1])
    curve_len = sum(math.dist(curve[i], curve[i - 1]) for i in range(1, len(curve))) or 1
    labels = []
    for c in found[:4]:
        name = c.get(lang) or c.get("en") or ""
        if c.get("lx") or c.get("ly"):
            labels.append((view.xy(c["lx"], c["ly"]), name.upper()))
    near = []  # the big countries around, named faintly, so the viewer knows where this is
    for c in countries():
        if c["a3"] in hl or not (c.get("lx") or c.get("ly")):
            continue
        x, y = view.xy(c["lx"], c["ly"])
        cw0, ch0 = bw / zoom, bh / zoom
        if not ((bw - cw0) / 2 + 60 < x < (bw + cw0) / 2 - 60 and (bh - ch0) / 2 + 40 < y < (bh + ch0) / 2 - 40):
            continue
        area = sum(_ring_area(view.ring(r)) for r in _main_rings(c))
        if area > cw0 * ch0 * 0.015:
            near.append((area, (x, y), (c.get(lang) or c.get("en") or "").upper()))
    near = [(p, nm) for _, p, nm in sorted(near, reverse=True)[:6] if nm]
    title = str(spec.get("title") or "").strip()[:48]
    vign = _vignette(w, h)
    n = max(1, round(seconds * fps))
    has_route, has_hl = len(curve) > 1, bool(outlines)
    # Timing (fractions of the scene): fly in, draw borders, fill, route, pins.
    t_fly = 0.45
    t_b0, t_b1 = 0.12, (0.5 if has_route else 0.65)
    t_f0, t_f1 = t_b1 - 0.12, t_b1 + 0.1
    t_r0, t_r1 = (t_f0 if has_hl else 0.2), 0.74
    enc = Encoder(out, w, h, fps, prev)
    try:
        for i in range(n):
            t = i / max(1, n - 1)
            # The camera: from the whole region down to the story, then a slow push.
            z = 1 + (zoom - 1) * _ease(_phase(t, 0, t_fly))
            z *= 1 + 0.05 * t
            cw, ch = bw / z, bh / z
            box = ((bw - cw) / 2, (bh - ch) / 2, (bw + cw) / 2, (bh + ch) / 2)
            frame = base.resize((w, h), Image.BILINEAR, box=box)
            sc = w / cw

            def to(p):
                return ((p[0] - box[0]) * sc, (p[1] - box[1]) * sc)

            fa = _ease(_phase(t, t_f0, t_f1)) * 0.55
            if has_hl and fa > 0:
                m = fill.resize((w, h), Image.BILINEAR, box=box).point(lambda v, fa=fa: int(v * fa))
                frame.paste(gold, (0, 0), m)
            lay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            d = ImageDraw.Draw(lay)
            glow_l = Image.new("L", (w // 2, h // 2), 0)
            gdraw = ImageDraw.Draw(glow_l)
            # Borders drawing on, all countries together along their length.
            bp = _out(_phase(t, t_b0, t_b1)) * total_outline
            for p, ln in outlines:
                take = min(ln, bp)
                bp -= take
                if take <= 0:
                    continue
                seg, acc = [to(p[0])], 0.0
                for k in range(1, len(p)):
                    step_ = math.dist(p[k], p[k - 1])
                    if acc + step_ > take:
                        f = (take - acc) / step_
                        seg.append(to((p[k - 1][0] + (p[k][0] - p[k - 1][0]) * f, p[k - 1][1] + (p[k][1] - p[k - 1][1]) * f)))
                        break
                    acc += step_
                    seg.append(to(p[k]))
                if len(seg) > 1:
                    d.line(seg, fill=(*GOLD, 255), width=4, joint="curve")
                    gdraw.line([(x / 2, y / 2) for x, y in seg], fill=200, width=6, joint="curve")
            places = _Labels()
            for p in route:
                places.block(*to(p), 14)
            for p, _ in pins:
                places.block(*to(p), 16)
            stop_labels = []
            for k, p in enumerate(route):
                if route_names[k]:
                    stop_labels.append(places.place(*to(p), route_names[k], _font(30)))
                else:
                    stop_labels.append(None)
            pin_labels = [places.place(*to(p), name, _font(32), gap=32) if name else None for p, name in pins]
            # Country names, only where they cover no place name.
            la = _ease(_phase(t, t_f0, t_f1))
            for p, name in labels:
                f = _font(max(26, int(34 * min(1.4, z / zoom + 0.4))), True)
                at = places.place(*to(p), name, f, gap=0, must=False)
                if at:
                    _text(d, at, name, f, alpha=la)
            na = _ease(_phase(t, t_fly * 0.6, t_fly + 0.1)) * 0.5
            if na > 0:
                fn = _font(24)
                for p, name in near:
                    x_, y_ = to(p)
                    half_ = fn.getlength(name) / 2
                    if x_ - half_ < 24 or x_ + half_ > w - 24 or not 70 < y_ < h - 30:
                        continue  # a name cut by the frame edge looks broken; leave it out
                    at = places.place(x_, y_, name, fn, gap=0, must=False)
                    if at:
                        _text(d, at, name, fn, fill=(170, 180, 198), alpha=na, shadow=2)
            # The route: an arc drawing itself with a bright head, the stops named as it passes them.
            if has_route:
                rp = _ease(_phase(t, t_r0, t_r1)) * curve_len
                seg, acc = [to(curve[0])], 0.0
                head = curve[0]
                for k in range(1, len(curve)):
                    step_ = math.dist(curve[k], curve[k - 1])
                    if acc + step_ >= rp:
                        f = (rp - acc) / step_ if step_ else 0
                        head = (curve[k - 1][0] + (curve[k][0] - curve[k - 1][0]) * f,
                                curve[k - 1][1] + (curve[k][1] - curve[k - 1][1]) * f)
                        seg.append(to(head))
                        break
                    acc += step_
                    seg.append(to(curve[k]))
                    head = curve[k]
                if rp > 0 and len(seg) > 1:
                    d.line(seg, fill=(255, 255, 255, 230), width=9, joint="curve")
                    d.line(seg, fill=(*RED, 255), width=5, joint="curve")
                    gdraw.line([(x / 2, y / 2) for x, y in seg], fill=255, width=8, joint="curve")
                    hx, hy = to(head)
                    bx, by = seg[-2] if math.dist(seg[-2], (hx, hy)) > 2 or len(seg) < 3 else seg[-3]
                    ang = math.atan2(hy - by, hx - bx)
                    tip = [(hx + 22 * math.cos(ang), hy + 22 * math.sin(ang)),
                           (hx + 13 * math.cos(ang + 2.4), hy + 13 * math.sin(ang + 2.4)),
                           (hx + 13 * math.cos(ang - 2.4), hy + 13 * math.sin(ang - 2.4))]
                    d.polygon(tip, fill=(*RED, 255), outline=(*WHITE, 255))
                    gdraw.polygon([(x / 2, y / 2) for x, y in tip], fill=200)
                for k in range(len(route)):
                    # Each stop pops in when the head passes it (the first one as the route starts).
                    reach = t_r0 + (t_r1 - t_r0) * k / max(1, len(route) - 1)
                    s = _back(_phase(t, reach - 0.02, reach + 0.08))
                    if s <= 0:
                        continue
                    x, y = to(route[k])
                    r_ = 8 * s
                    d.ellipse([x - r_, y - r_, x + r_, y + r_], fill=(*RED, 255), outline=(*WHITE, 255), width=3)
                    if stop_labels[k]:
                        _text(d, stop_labels[k], route_names[k], _font(30), alpha=min(1.0, s))
            # Pins drop one by one.
            for k, (p, name) in enumerate(pins):
                s = _back(_phase(t, 0.5 + 0.08 * k, 0.6 + 0.08 * k))
                if s <= 0:
                    continue
                x, y = to(p)
                y -= 18 * (1 - min(s, 1))
                d.ellipse([x - 22 * s, y - 22 * s, x + 22 * s, y + 22 * s], outline=(*GOLD, int(120 * min(s, 1))), width=3)
                d.ellipse([x - 9 * s, y - 9 * s, x + 9 * s, y + 9 * s], fill=(*WHITE, 255), outline=(*RED, 255), width=4)
                if pin_labels[k]:
                    _text(d, (pin_labels[k][0], pin_labels[k][1] - 18 * (1 - min(s, 1))), name, _font(32),
                          alpha=min(1.0, s))
            glow_rgba = Image.merge("RGBA", (*Image.new("RGB", glow_l.size, GOLD).split(),
                                             glow_l.filter(ImageFilter.GaussianBlur(7))))
            frame = frame.convert("RGBA")
            frame.alpha_composite(glow_rgba.resize((w, h), Image.BILINEAR))
            frame.alpha_composite(lay)
            frame = ImageChops.multiply(frame.convert("RGB"), vign)
            if title:
                frame = _caption(frame, title, _phase(t, 0.08, 0.2))
            enc.add(frame)
            if poster and i == n - 1:
                frame.save(poster)
        enc.close()
    except BaseException:
        enc.abort()
        raise


def _caption(frame: Image.Image, text: str, p: float) -> Image.Image:
    """A title plate sliding in at the top left."""
    if p <= 0:
        return frame
    w, h = frame.size
    f = _font(int(h * 0.042), True)
    tw = int(f.getlength(text))
    x = int(h * 0.06 - (1 - _out(p)) * (tw + 120))
    y = int(h * 0.07)
    lay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    pad = int(h * 0.018)
    d.rounded_rectangle([x - pad, y - pad, x + tw + pad, y + int(h * 0.05) + pad], radius=10, fill=(8, 10, 16, 190))
    d.rectangle([x - pad, y - pad, x - pad + 6, y + int(h * 0.05) + pad], fill=(*GOLD, 255))
    d.text((x + 8, y), text, font=f, fill=(*WHITE, 255))
    out = frame.convert("RGBA")
    out.alpha_composite(lay)
    return out.convert("RGB")


# ---------------------------------------------------------------- timeline

def timeline_ok(spec: dict) -> bool:
    ev = [e for e in spec.get("events") or [] if isinstance(e, dict) and str(e.get("label") or "").strip()]
    return 2 <= len(ev) <= 7


def render_timeline(spec: dict, out: Path, seconds: float, w: int, h: int, fps: int, poster: Path = None,
                    prev: Image.Image = None):
    events = [e for e in spec.get("events") or [] if isinstance(e, dict) and str(e.get("label") or "").strip()][:7]
    try:
        cur = int(spec.get("current", len(events) - 1))
    except (TypeError, ValueError):
        cur = len(events) - 1
    cur = max(0, min(len(events) - 1, cur))
    pad = 1.12  # the background is a little bigger, for a slow drift
    bw, bh = int(w * pad), int(h * pad)
    bg = _gradient(bw, bh, BG_TOP, BG_BOTTOM)
    if prev is not None:
        soft = _cover(prev, bw // 8, bh // 8).filter(ImageFilter.GaussianBlur(6))
        bg = Image.blend(bg, soft.resize((bw, bh), Image.BICUBIC).point(lambda v: int(v * 0.32)), 0.6)
    gd = ImageDraw.Draw(bg)
    for x in range(0, bw, int(h * 0.09)):  # a faint grid
        gd.line([(x, 0), (x, bh)], fill=(24, 32, 48), width=1)
    for y in range(0, bh, int(h * 0.09)):
        gd.line([(0, y), (bw, y)], fill=(24, 32, 48), width=1)
    vign = _vignette(w, h, 0.6)
    n = max(1, round(seconds * fps))
    m = len(events)
    left, right = w * 0.1, w * 0.9
    xs = [left + (right - left) * (k + 0.5) / m for k in range(m)]
    y0 = h * 0.56
    f_year = _font(int(h * 0.07), True)
    f_year_cur = _font(int(h * 0.095), True)
    f_txt = _font(int(h * 0.033))
    title = str(spec.get("title") or "").strip()[:48]
    slot = (right - left) / m * 0.92
    texts = [_wrap(str(e.get("text") or "").strip(), f_txt, int(slot), 3) for e in events]
    line_end = 0.12 + 0.5 * (cur + 1) / m  # the line runs as far as the current event, then to the end
    enc = Encoder(out, w, h, fps, prev)
    try:
        for i in range(n):
            t = i / max(1, n - 1)
            drift = t * (bw - w) * 0.6
            frame = bg.crop((int(drift), int((bh - h) / 2), int(drift) + w, int((bh - h) / 2) + h)).convert("RGBA")
            lay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            d = ImageDraw.Draw(lay)
            glow = Image.new("L", (w // 2, h // 2), 0)
            g = ImageDraw.Draw(glow)
            lp = _ease(_phase(t, 0.05, line_end))
            lp2 = _ease(_phase(t, line_end + 0.1, 0.95))
            x_end = left + (xs[cur] - left) * lp if lp < 1 else xs[cur] + (right - xs[cur]) * lp2
            d.line([(left, y0), (right, y0)], fill=(60, 70, 90, 255), width=3)
            d.line([(left, y0), (x_end, y0)], fill=(*GOLD, 255), width=6)
            g.line([(left / 2, y0 / 2), (x_end / 2, y0 / 2)], fill=180, width=6)
            for k, (x, e) in enumerate(zip(xs, events)):
                reach = (x - left) / max(1, xs[cur] - left) if k <= cur else 1.0
                appear = (0.05 + (line_end - 0.05) * reach) if k <= cur else (line_end + 0.1 + (0.85 - line_end) * (k - cur) / max(1, m - cur))
                s = _back(_phase(t, appear, appear + 0.08))
                if s <= 0:
                    continue
                is_cur = k == cur
                r = (16 if is_cur else 11) * s
                if is_cur:
                    pulse = 0.5 + 0.5 * math.sin(t * math.pi * 6)
                    rr = 30 + 10 * pulse
                    d.ellipse([x - rr, y0 - rr, x + rr, y0 + rr], outline=(*GOLD, int(110 * min(s, 1))), width=3)
                    g.ellipse([(x - 30) / 2, (y0 - 30) / 2, (x + 30) / 2, (y0 + 30) / 2], fill=255)
                d.ellipse([x - r, y0 - r, x + r, y0 + r], fill=(*(GOLD if k <= cur else (150, 160, 180)), 255),
                          outline=(*WHITE, 255), width=3)
                a = min(1.0, s)
                lift = 20 * (1 - a)
                _text(d, (x, y0 - h * (0.12 if is_cur else 0.1) + lift), str(e.get("label"))[:14],
                      f_year_cur if is_cur else f_year, fill=GOLD if is_cur else WHITE, alpha=a)
                for j, line in enumerate(texts[k]):
                    _text(d, (x, y0 + h * 0.07 + j * h * 0.038 - lift), line, f_txt,
                          fill=WHITE if is_cur else (190, 198, 212), alpha=a * (1.0 if is_cur else 0.85), shadow=2)
            frame.alpha_composite(Image.merge("RGBA", (*Image.new("RGB", glow.size, GOLD).split(),
                                                       glow.filter(ImageFilter.GaussianBlur(8)))).resize((w, h)))
            frame.alpha_composite(lay)
            img = ImageChops.multiply(frame.convert("RGB"), vign)
            if title:
                img = _caption(img, title, _phase(t, 0.03, 0.15))
            enc.add(img)
            if poster and i == n - 1:
                img.save(poster)
        enc.close()
    except BaseException:
        enc.abort()
        raise


# ---------------------------------------------------------------- charts, comparisons, quotes, lists

def _num(v):
    try:
        return float(str(v).replace(" ", "").replace(",", ".").replace("−", "-"))
    except (TypeError, ValueError):
        return None


def _fmt(v: float, unit: str = "") -> str:
    a = abs(v)
    if a >= 100 or float(v).is_integer():
        s = f"{v:,.0f}".replace(",", " ")
    elif a >= 10:
        s = f"{v:.1f}".replace(".", ",")
    else:
        s = f"{v:.2f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{s} {unit}".strip() if unit and unit not in ("%",) else s + (unit or "")


def _count(v: float, p: float) -> float:
    """A value counting up: whole numbers stay whole on the way (no 29,6 on the way to 56)."""
    x = v * p
    return float(round(x)) if float(v).is_integer() else x


def _ticks(lo: float, hi: float, n: int = 4) -> list:
    """Round grid values (0, 50, 100, 150) covering lo..hi."""
    span = (hi - lo) or abs(hi) or 1
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    a = math.floor(lo / step) * step
    out = []
    while a <= hi + step * 0.999 and len(out) < 12:
        out.append(round(a, 10))
        a += step
    return out


class _Stage:
    """The dark studio background shared by charts, comparisons, quotes and lists: a soft gradient with a faint
    grid that drifts slowly, a vignette and a title plate."""

    def __init__(self, w, h, title="", backdrop: Image.Image = None):
        self.w, self.h, self.title = w, h, str(title or "").strip()[:48]
        pad = 1.12
        self.bw, self.bh = int(w * pad), int(h * pad)
        bg = _gradient(self.bw, self.bh, BG_TOP, BG_BOTTOM)
        if backdrop is not None:  # the colours of the picture before, deeply blurred and dark: the video stays one piece
            soft = _cover(backdrop, self.bw // 8, self.bh // 8).filter(ImageFilter.GaussianBlur(6))
            soft = soft.resize((self.bw, self.bh), Image.BICUBIC).point(lambda v: int(v * 0.32))
            bg = Image.blend(bg, soft, 0.6)
        gd = ImageDraw.Draw(bg)
        for x in range(0, self.bw, int(h * 0.09)):
            gd.line([(x, 0), (x, self.bh)], fill=(24, 32, 48), width=1)
        for y in range(0, self.bh, int(h * 0.09)):
            gd.line([(0, y), (self.bw, y)], fill=(24, 32, 48), width=1)
        self.bg = bg
        self.vign = _vignette(w, h, 0.6)

    def begin(self, t):
        drift = t * (self.bw - self.w) * 0.5
        top = int((self.bh - self.h) / 2)
        frame = self.bg.crop((int(drift), top, int(drift) + self.w, top + self.h)).convert("RGBA")
        lay = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
        glow = Image.new("L", (self.w // 2, self.h // 2), 0)
        return frame, lay, ImageDraw.Draw(lay), glow, ImageDraw.Draw(glow)

    def end(self, t, frame, lay, glow, color=GOLD):
        frame.alpha_composite(Image.merge("RGBA", (*Image.new("RGB", glow.size, color).split(),
                                                   glow.filter(ImageFilter.GaussianBlur(8)))).resize((self.w, self.h)))
        frame.alpha_composite(lay)
        img = ImageChops.multiply(frame.convert("RGB"), self.vign)
        if self.title:
            img = _caption(img, self.title, _phase(t, 0.03, 0.15))
        return img


def _frames(spec, out, seconds, w, h, fps, poster, draw, color=GOLD, prev: Image.Image = None):
    stage = _Stage(w, h, spec.get("title"), prev)
    n = max(1, round(seconds * fps))
    enc = Encoder(out, w, h, fps, prev)
    try:
        for i in range(n):
            t = i / max(1, n - 1)
            frame, lay, d, glow, g = stage.begin(t)
            draw(t, d, g)
            img = stage.end(t, frame, lay, glow, color)
            enc.add(img)
            if poster and i == n - 1:
                img.save(poster)
        enc.close()
    except BaseException:
        enc.abort()
        raise


def _chart_points(spec):
    out = []
    for p in spec.get("points") or []:
        if isinstance(p, dict) and _num(p.get("value")) is not None and str(p.get("label") or "").strip():
            out.append((str(p["label"]).strip()[:12], _num(p["value"])))
    return out[:10]


def chart_ok(spec: dict) -> bool:
    return len(_chart_points(spec)) >= 2


def render_chart(spec, out, seconds, w, h, fps, poster=None, prev=None):
    """A line that draws itself through the values, or bars that grow one by one; the values count up."""
    pts = _chart_points(spec)
    unit = str(spec.get("unit") or "").strip()[:10]
    bars = spec.get("chart_style") == "bar" or len(pts) <= 3
    vmax = max(v for _, v in pts)
    grid = _ticks(min(0.0, min(v for _, v in pts)), vmax * 1.08 if vmax > 0 else 1)
    vmin, top = grid[0], grid[-1]
    left, right, y_top, y_bot = w * 0.12, w * 0.9, h * 0.24, h * 0.8
    m = len(pts)
    xs = [left + (right - left) * (k + 0.5) / m for k in range(m)]

    def y_of(v):
        return y_bot - (v - vmin) / (top - vmin) * (y_bot - y_top)

    f_val, f_big, f_lab = _font(int(h * 0.036), True), _font(int(h * 0.075), True), _font(int(h * 0.03))

    def draw(t, d, g):
        ga = _ease(_phase(t, 0.02, 0.15))
        for gv in grid:  # the grid with its values
            y = y_of(gv)
            d.line([(left, y), (right, y)], fill=(70, 80, 100, int(140 * ga)), width=1)
            _text(d, (left - 16, y), _fmt(gv), _font(int(h * 0.024)), fill=MUTED, anchor="rm", alpha=ga, shadow=0)
        d.line([(left, y_bot), (right, y_bot)], fill=(120, 130, 150, int(255 * ga)), width=3)
        for k, (lab, _) in enumerate(pts):
            _text(d, (xs[k], y_bot + h * 0.04), lab, f_lab, fill=(200, 206, 218), alpha=ga, shadow=0)
        if bars:
            bw = (right - left) / m * 0.5
            for k, (lab, v) in enumerate(pts):
                p = _out(_phase(t, 0.12 + 0.55 * k / m, 0.12 + 0.55 * (k + 1) / m + 0.1))
                if p <= 0:
                    continue
                last = k == m - 1
                y = y_of(vmin + (v - vmin) * p)
                col = GOLD if last else (180, 140, 30)
                d.rounded_rectangle([xs[k] - bw / 2, y, xs[k] + bw / 2, y_bot], radius=8, fill=(*col, 255))
                if last:
                    g.rectangle([(xs[k] - bw / 2) / 2, y / 2, (xs[k] + bw / 2) / 2, y_bot / 2], fill=150)
                _text(d, (xs[k], y - h * 0.035), _fmt(_count(v, p), unit), f_big if last else f_val, fill=GOLD if last else WHITE)
        else:
            p = _ease(_phase(t, 0.12, 0.8)) * (m - 1)
            line = []
            for k in range(m):
                if k <= p:
                    line.append((xs[k], y_of(pts[k][1])))
                elif k - 1 < p:
                    f = p - (k - 1)
                    line.append((xs[k - 1] + (xs[k] - xs[k - 1]) * f,
                                 y_of(pts[k - 1][1] + (pts[k][1] - pts[k - 1][1]) * f)))
            if len(line) > 1:
                area = line + [(line[-1][0], y_bot), (line[0][0], y_bot)]
                d.polygon(area, fill=(*GOLD, 40))
                d.line(line, fill=(*GOLD, 255), width=7, joint="curve")
                g.line([(x / 2, y / 2) for x, y in line], fill=220, width=7)
            for k in range(m):
                s = _back(_phase(t, 0.12 + 0.68 * k / max(1, m - 1) - 0.02, 0.12 + 0.68 * k / max(1, m - 1) + 0.08))
                if s <= 0:
                    continue
                x, y = xs[k], y_of(pts[k][1])
                last = k == m - 1
                r = (14 if last else 9) * s
                d.ellipse([x - r, y - r, x + r, y + r], fill=(*WHITE, 255), outline=(*GOLD, 255), width=4)
                _text(d, (x, y - h * (0.06 if last else 0.04)), _fmt(pts[k][1], unit), f_big if last else f_val,
                      fill=GOLD if last else WHITE, alpha=min(1.0, s))

    _frames(spec, out, seconds, w, h, fps, poster, draw, prev=prev)


def _rows(spec):
    out = []
    for r in spec.get("rows") or []:
        if isinstance(r, dict) and str(r.get("label") or "").strip():
            a, b = _num(r.get("left")), _num(r.get("right"))
            if a is not None and b is not None and (a > 0 or b > 0):
                out.append((str(r["label"]).strip()[:24], max(0.0, a), max(0.0, b)))
    return out[:5]


def compare_ok(spec: dict) -> bool:
    return len(_rows(spec)) >= 1 and bool(str(spec.get("left_name") or "").strip()) \
        and bool(str(spec.get("right_name") or "").strip())


def render_compare(spec, out, seconds, w, h, fps, poster=None, prev=None):
    """Two sides head to head: a bar for each grows out from the middle, the bigger one shines."""
    rows = _rows(spec)
    unit = str(spec.get("unit") or "").strip()[:10]
    ln, rn = str(spec.get("left_name") or "").strip()[:20], str(spec.get("right_name") or "").strip()[:20]
    cx = w / 2
    half = w * 0.33
    y0 = h * 0.36
    step = min(h * 0.14, (h * 0.5) / max(1, len(rows)))
    f_name, f_lab, f_val = _font(int(h * 0.06), True), _font(int(h * 0.03)), _font(int(h * 0.04), True)

    def draw(t, d, g):
        a = _out(_phase(t, 0.02, 0.15))
        _text(d, (cx - half * 0.55 - (1 - a) * 200, h * 0.22), ln.upper(), f_name, fill=GOLD, alpha=a)
        _text(d, (cx + half * 0.55 + (1 - a) * 200, h * 0.22), rn.upper(), f_name, fill=BLUE, alpha=a)
        _text(d, (cx, h * 0.22), "VS", _font(int(h * 0.045), True), fill=MUTED, alpha=a)
        for k, (lab, lv, rv) in enumerate(rows):
            y = y0 + step * (k + 0.5)
            p = _out(_phase(t, 0.15 + 0.12 * k, 0.55 + 0.12 * k))
            ra = _ease(_phase(t, 0.1 + 0.12 * k, 0.2 + 0.12 * k))
            mx = max(lv, rv) or 1
            bh = step * 0.32
            _text(d, (cx, y - step * 0.36), lab, f_lab, fill=(210, 216, 228), alpha=ra, shadow=0)
            d.rounded_rectangle([cx - half, y - bh / 2, cx + half, y + bh / 2], radius=int(bh / 2),
                                fill=(40, 48, 64, int(200 * ra)))
            lw, rw = half * lv / mx * p, half * rv / mx * p
            win_l, win_r = lv > rv, rv > lv
            if lw > 1:
                d.rounded_rectangle([cx - lw, y - bh / 2, cx, y + bh / 2], radius=int(bh / 2),
                                    fill=(*(GOLD if win_l or lv == rv else (150, 120, 40)), 255))
                if win_l:
                    g.rectangle([(cx - lw) / 2, (y - bh / 2) / 2, cx / 2, (y + bh / 2) / 2], fill=140)
            if rw > 1:
                d.rounded_rectangle([cx, y - bh / 2, cx + rw, y + bh / 2], radius=int(bh / 2),
                                    fill=(*(BLUE if win_r or lv == rv else (60, 90, 140)), 255))
            d.line([(cx, y - bh), (cx, y + bh)], fill=(*WHITE, int(200 * ra)), width=3)
            _text(d, (cx - half - 16, y), _fmt(_count(lv, p), unit), f_val, fill=GOLD if win_l else WHITE, anchor="rm", alpha=ra)
            _text(d, (cx + half + 16, y), _fmt(_count(rv, p), unit), f_val, fill=BLUE if win_r else WHITE, anchor="lm", alpha=ra)

    _frames(spec, out, seconds, w, h, fps, poster, draw, prev=prev)


def quote_ok(spec: dict) -> bool:
    q = str(spec.get("quote") or "").strip()
    return 3 <= len(q.split()) <= 40


def render_quote(spec, out, seconds, w, h, fps, poster=None, prev=None):
    """A quote typed out word by word, big, with the author sliding in under a gold line."""
    quote = str(spec.get("quote") or "").strip().strip("«»\"“”")
    author = str(spec.get("author") or "").strip()[:60]
    size = int(h * (0.07 if len(quote) < 80 else 0.058 if len(quote) < 160 else 0.048))
    f = _font(size, True)
    lines = _wrap(quote, f, int(w * 0.72), 6)
    words_total = sum(len(x.split()) for x in lines) or 1
    lh = size * 1.32
    y_start = h * 0.5 - lh * len(lines) / 2
    f_auth = _font(int(h * 0.036))

    def draw(t, d, g):
        qa = _out(_phase(t, 0.0, 0.12))
        _text(d, (w * 0.12, y_start - size * 0.6), "“", _font(int(h * 0.22), True), fill=GOLD, alpha=qa * 0.9, shadow=0)
        shown = _phase(t, 0.08, 0.7) * words_total
        k = 0
        for j, line in enumerate(lines):
            words = line.split()
            x = w * 0.5 - f.getlength(line) / 2
            y = y_start + lh * (j + 0.5)
            for wd in words:
                a = min(1.0, max(0.0, shown - k))
                if a > 0:
                    _text(d, (x, y + 10 * (1 - a)), wd, f, anchor="lm", alpha=a)
                x += f.getlength(wd + " ")
                k += 1
        if author:
            p = _out(_phase(t, 0.72, 0.88))
            yl = y_start + lh * len(lines) + h * 0.04
            d.line([(w / 2 - 60 * p, yl), (w / 2 + 60 * p, yl)], fill=(*GOLD, 255), width=4)
            g.line([((w / 2 - 60 * p) / 2, yl / 2), ((w / 2 + 60 * p) / 2, yl / 2)], fill=200, width=4)
            _text(d, (w / 2, yl + h * 0.05 + 20 * (1 - p)), author, f_auth, fill=(210, 216, 228), alpha=p)

    _frames(spec, out, seconds, w, h, fps, poster, draw, prev=prev)


def _lines(spec):
    return [str(x).strip()[:60] for x in spec.get("lines") or [] if str(x).strip()][:6]


def list_ok(spec: dict) -> bool:
    return 2 <= len(_lines(spec)) <= 6


def render_list(spec, out, seconds, w, h, fps, poster=None, prev=None):
    """Numbered points sliding in one by one; the newest one is bright, the earlier ones step back a little."""
    items = _lines(spec)
    m = len(items)
    f = _font(int(h * (0.06 if m <= 3 else 0.052 if m <= 4 else 0.044)), True)
    f_num = _font(int(h * 0.048), True)
    step = min(h * 0.15, h * 0.64 / m)
    y0 = h * 0.5 - step * m / 2 + step / 2 + h * 0.03
    widest = max(f.getlength(_wrap(x, f, int(w * 0.66), 1)[0]) for x in items)
    x0 = max(w * 0.1, (w - widest - step * 0.6 - 30) / 2 + step * 0.3)

    def draw(t, d, g):
        for k, txt in enumerate(items):
            a0 = 0.1 + 0.7 * k / m
            p = _out(_phase(t, a0, a0 + 0.12))
            if p <= 0:
                continue
            y = y0 + step * k
            newest = k == min(m - 1, int((t - 0.1) / 0.7 * m)) if t >= 0.1 else False
            x = x0 - (1 - p) * 120
            r = step * 0.3
            d.ellipse([x - r, y - r, x + r, y + r], fill=(*(GOLD if newest else (120, 100, 40)), int(255 * p)))
            if newest:
                g.ellipse([(x - r) / 2, (y - r) / 2, (x + r) / 2, (y + r) / 2], fill=220)
            _text(d, (x, y), str(k + 1), f_num, fill=(12, 14, 20), alpha=p, shadow=0)
            _text(d, (x + r + 30, y), _wrap(txt, f, int(w * 0.66), 1)[0], f, anchor="lm",
                  fill=WHITE if newest else (200, 206, 218), alpha=p)

    _frames(spec, out, seconds, w, h, fps, poster, draw, prev=prev)


def render(spec: dict, out: Path, seconds: float, w: int, h: int, fps: int, lang: str = "ru", poster: Path = None,
           prev: Path = None):
    """prev: the picture of the scene before, for a soft way in (and the background colours)."""
    pic = None
    if prev:
        try:
            pic = Image.open(prev).convert("RGB")
        except OSError:
            pic = None
    if spec.get("type") == "map":
        render_map(spec, out, seconds, w, h, fps, lang, poster, pic)
    elif spec.get("type") == "timeline":
        render_timeline(spec, out, seconds, w, h, fps, poster, pic)
    elif spec.get("type") in DRAW:
        DRAW[spec["type"]](spec, out, seconds, w, h, fps, poster, pic)
    else:
        raise UserError(f"Неизвестный вид моушена: {spec.get('type')}")


def cues(spec: dict, seconds: float) -> list:
    """Moments (seconds from the scene start) for soft sounds that match what appears: pops for points
    and pins, ticks for list items and rows. Kept to the renderers' own timing."""
    kind, out = spec.get("type"), []
    if kind == "map":
        route = _points(spec, "route")
        has_hl = any(find_country(x) for x in spec.get("countries") or [])
        t_b1 = 0.5 if len(route) > 1 else 0.65
        t_r0 = (t_b1 - 0.12) if has_hl else 0.2
        if len(route) > 1:
            out += [(t_r0 + (0.74 - t_r0) * k / (len(route) - 1), "pop") for k in range(len(route))]
        out += [(0.5 + 0.08 * k + 0.02, "pop") for k in range(len(_points(spec, "pins")))]
    elif kind == "timeline":
        ev = [e for e in spec.get("events") or [] if isinstance(e, dict) and str(e.get("label") or "").strip()][:7]
        m = len(ev)
        if m:
            try:
                cur = max(0, min(m - 1, int(spec.get("current", m - 1))))
            except (TypeError, ValueError):
                cur = m - 1
            line_end = 0.12 + 0.5 * (cur + 1) / m
            for k in range(m):
                if k <= cur:
                    reach = (k + 0.5) / (cur + 0.5) if cur else 1.0
                    out.append((0.05 + (line_end - 0.05) * reach, "pop"))
                else:
                    out.append((line_end + 0.1 + (0.85 - line_end) * (k - cur) / max(1, m - cur), "pop"))
    elif kind == "chart":
        m = len(_chart_points(spec))
        bars = spec.get("chart_style") == "bar" or m <= 3
        out += [((0.12 + 0.55 * k / m) if bars else (0.12 + 0.68 * k / max(1, m - 1)), "pop") for k in range(m)]
    elif kind == "compare":
        out += [(0.15 + 0.12 * k, "tick") for k in range(len(_rows(spec)))]
    elif kind == "list":
        m = len(_lines(spec))
        out += [(0.1 + 0.7 * k / m, "tick") for k in range(m)]
    elif kind == "quote" and str(spec.get("author") or "").strip():
        out.append((0.72, "tick"))
    # Never two sounds closer than a fifth of a second, and none in the first half second (the whoosh).
    res = []
    for f, name in sorted(out):
        at = f * seconds
        if at >= 0.5 and at < seconds - 0.1 and (not res or at - res[-1][0] >= 0.2):
            res.append((round(at, 3), name))
    return res[:12]


DRAW = {"chart": render_chart, "compare": render_compare, "quote": render_quote, "list": render_list}
CHECK = {"map": map_ok, "timeline": timeline_ok, "chart": chart_ok, "compare": compare_ok, "quote": quote_ok,
         "list": list_ok}


def poster(spec: dict, path: Path, w: int, h: int, lang: str = "ru"):
    """The last frame as the scene's picture (shown in the scene list; a short clip is drawn and thrown away)."""
    tmp = path.with_suffix(".poster.mp4")
    try:
        render(spec, tmp, 0.4, w, h, 10, lang, poster=path)
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------- the plan

FIELDS = ("index", "type", "title", "countries", "route", "pins", "events", "current", "points", "chart_style", "unit",
          "left_name", "right_name", "rows", "quote", "author", "lines")
_PLACE = {"type": "object", "properties": {"name": {"type": "string"}, "lat": {"type": "number"},
                                           "lon": {"type": "number"}}, "required": ["name", "lat", "lon"]}
SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
    "index": {"type": "integer"}, "type": {"type": "string", "enum": list(KINDS)},
    "title": {"type": "string"},
    "countries": {"type": "array", "items": {"type": "string"}},
    "route": {"type": "array", "items": _PLACE},
    "pins": {"type": "array", "items": _PLACE},
    "events": {"type": "array", "items": {"type": "object", "properties": {
        "label": {"type": "string"}, "text": {"type": "string"}}, "required": ["label", "text"]}},
    "current": {"type": "integer"},
    "points": {"type": "array", "items": {"type": "object", "properties": {
        "label": {"type": "string"}, "value": {"type": "number"}}, "required": ["label", "value"]}},
    "chart_style": {"type": "string", "enum": ["line", "bar"]},
    "unit": {"type": "string"},
    "left_name": {"type": "string"}, "right_name": {"type": "string"},
    "rows": {"type": "array", "items": {"type": "object", "properties": {
        "label": {"type": "string"}, "left": {"type": "number"}, "right": {"type": "number"}},
        "required": ["label", "left", "right"]}},
    "quote": {"type": "string"}, "author": {"type": "string"},
    "lines": {"type": "array", "items": {"type": "string"}}},
    "required": list(FIELDS)}}},
    "required": ["items"]}

PLAN_PROMPT = """You are the motion designer of a narrated YouTube video. Some scenes will show an animation INSTEAD of a picture, where it explains the moment better than a picture:
- "map": the narration names countries, a territory, a conquest, a journey, a trade route, a migration, borders, where something is ("a tiny island off the coast of Africa").
- "timeline": several dates in a row, a chain of events ("in 1890... five years later... by 1936").
- "chart": numbers that change over time or several values to compare in one series (population grew from... to..., prices, army size by year).
- "compare": two sides head to head on one or more measures (Italy vs Ethiopia: soldiers, cannons, losses).
- "quote": a real, well-known quote said by a named person in the story, worth showing word for word.
- "list": the narration counts points ("three reasons", "first... second... third...", a top).

Choose up to {limit} scenes, mixing kinds where the story allows. Only where the animation is the point of that moment; never two such scenes in a row; never the first or the last scene. Choose none if nothing fits.

Fill only the fields of the chosen kind; leave the others empty ("" / [] / 0, chart_style "line"):
- index: the scene number from the list. title: a short plate in {language}, max 5 words ("Эфиопия, 1896", "Армии при Адуа").
- map: countries = modern countries to highlight (English names), the closest modern equivalent of the place in the story; route = 2-6 stops in order with real coordinates (lat, lon); pins = 0-4 places with real coordinates. Names in {language}, max 3 words.
- timeline: events = 3-6 dates in order, label = the date as said ("1896", "XV век"), text = what happened, max 7 words, in {language}; current = the index of the event this scene is about.
- chart: points = 2-8 values in order, label = year or name (max 12 characters), value = the number; chart_style "line" for change over time, "bar" for separate items; unit = "%", "млн", "$", "чел." or "".
- compare: left_name and right_name = the two sides (max 2 words); rows = 1-4 measures, label = what is measured, left and right = the numbers; unit if the same for all rows.
- quote: quote = the exact words in {language}, max 30 words; author = who said it and when ("Менелик II, 1896").
- list: lines = 2-6 points, max 8 words each, in {language}.
Never invent facts or numbers: only what the narration says or certain common knowledge. If the numbers are not known for sure, do not make a chart or a comparison.

SCENES:
{scenes}"""


def plan_prompt(scenes: list, language: str, total_seconds: float) -> str:
    limit = max(1, min(14, round(total_seconds / 75)))
    rows = "\n".join(f"{i}. {sc.get('text', '')}" for i, sc in enumerate(scenes))
    return PLAN_PROMPT.format(limit=limit, language=language, scenes=rows)


def clean_plan(items: list, n_scenes: int) -> list:
    """Drops what cannot be drawn (unknown places, too few dates or numbers), the first and the last scene, neighbours."""
    out, used = [], set()
    for g in sorted(items or [], key=lambda x: int(x.get("index", -1)) if isinstance(x, dict) else -1):
        if not isinstance(g, dict):
            continue
        try:
            i = int(g.get("index"))
        except (TypeError, ValueError):
            continue
        if not 0 < i < n_scenes - 1 or i in used or i - 1 in used or i + 1 in used:
            continue
        if g.get("type") not in KINDS or not CHECK[g["type"]](g):
            continue
        out.append({k: g.get(k) for k in FIELDS})
        used.add(i)
    return out
