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
KINDS = {"map": "🗺 Карта", "timeline": "📅 Линия времени"}
MODES = {"auto": "Авто: карты и даты там, где они к месту", "off": "Выключено"}

OCEAN_TOP, OCEAN_BOTTOM = (12, 22, 40), (6, 12, 24)
LAND, LAND_EDGE, BORDER = (52, 60, 74), (92, 104, 124), (78, 88, 106)
GOLD, GOLD_DEEP, RED, WHITE = (250, 204, 21), (212, 160, 23), (239, 68, 68), (255, 255, 255)
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

    def __init__(self, out: Path, w: int, h: int, fps: int):
        self.out, self.tmp = out, out.with_suffix(".motion.mp4")
        self.p = subprocess.Popen([media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
                                   "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
                                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                                   "-r", str(fps), str(self.tmp)],
                                  stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    def add(self, frame: Image.Image):
        self.p.stdin.write(frame.convert("RGB").tobytes())

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
               poster: Path = None):
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
    title = str(spec.get("title") or "").strip()[:48]
    vign = _vignette(w, h)
    n = max(1, round(seconds * fps))
    has_route, has_hl = len(curve) > 1, bool(outlines)
    # Timing (fractions of the scene): fly in, draw borders, fill, route, pins.
    t_fly = 0.45
    t_b0, t_b1 = 0.12, (0.5 if has_route else 0.65)
    t_f0, t_f1 = t_b1 - 0.12, t_b1 + 0.1
    t_r0, t_r1 = (t_f0 if has_hl else 0.2), 0.74
    enc = Encoder(out, w, h, fps)
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
                    d.ellipse([hx - 9, hy - 9, hx + 9, hy + 9], fill=(*WHITE, 255), outline=(*RED, 255), width=4)
                stops_done = 0
                acc = 0.0
                for k, rpt in enumerate(route):
                    at = k / max(1, len(route) - 1) * curve_len
                    if rp + 1 >= at:
                        stops_done += 1
                for k in range(min(stops_done, len(route))):
                    x, y = to(route[k])
                    s = _back(min(1.0, (rp - k / max(1, len(route) - 1) * curve_len) / (curve_len * 0.08) + 0.0001))
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


def render_timeline(spec: dict, out: Path, seconds: float, w: int, h: int, fps: int, poster: Path = None):
    events = [e for e in spec.get("events") or [] if isinstance(e, dict) and str(e.get("label") or "").strip()][:7]
    try:
        cur = int(spec.get("current", len(events) - 1))
    except (TypeError, ValueError):
        cur = len(events) - 1
    cur = max(0, min(len(events) - 1, cur))
    pad = 1.12  # the background is a little bigger, for a slow drift
    bw, bh = int(w * pad), int(h * pad)
    bg = _gradient(bw, bh, BG_TOP, BG_BOTTOM)
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
    enc = Encoder(out, w, h, fps)
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


def render(spec: dict, out: Path, seconds: float, w: int, h: int, fps: int, lang: str = "ru", poster: Path = None):
    if spec.get("type") == "map":
        render_map(spec, out, seconds, w, h, fps, lang, poster)
    elif spec.get("type") == "timeline":
        render_timeline(spec, out, seconds, w, h, fps, poster)
    else:
        raise UserError(f"Неизвестный вид моушена: {spec.get('type')}")


def poster(spec: dict, path: Path, w: int, h: int, lang: str = "ru"):
    """The last frame as the scene's picture (shown in the scene list; a short clip is drawn and thrown away)."""
    tmp = path.with_suffix(".poster.mp4")
    try:
        render(spec, tmp, 0.4, w, h, 10, lang, poster=path)
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------- the plan

SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
    "index": {"type": "integer"}, "type": {"type": "string", "enum": ["map", "timeline"]},
    "title": {"type": "string"},
    "countries": {"type": "array", "items": {"type": "string"}},
    "route": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string"}, "lat": {"type": "number"}, "lon": {"type": "number"}},
        "required": ["name", "lat", "lon"]}},
    "pins": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string"}, "lat": {"type": "number"}, "lon": {"type": "number"}},
        "required": ["name", "lat", "lon"]}},
    "events": {"type": "array", "items": {"type": "object", "properties": {
        "label": {"type": "string"}, "text": {"type": "string"}}, "required": ["label", "text"]}},
    "current": {"type": "integer"}},
    "required": ["index", "type", "title", "countries", "route", "pins", "events", "current"]}}},
    "required": ["items"]}

PLAN_PROMPT = """You are the motion designer of a narrated YouTube video. Some scenes will show an animated map or an animated timeline INSTEAD of a picture, where it helps the viewer more than a picture: the narration names countries, a territory, a conquest, a journey, a trade route, a migration, borders changing, where something is ("this tiny island off the coast of Africa"), or several dates in a row, a chain of events, "in 1890... five years later... by 1936".

Choose up to {limit} scenes. Only where the map or the dates are the point of that moment; never two such scenes in a row; never the first or the last scene. Choose none if nothing fits.

For each chosen scene:
- index: the scene number from the list.
- type "map": countries = modern countries to highlight (English names) as the closest modern equivalent of the place in the story (for an old empire, the modern countries it covered); route = 2-6 stops of a journey or movement in order, with real coordinates (lat, lon in degrees); pins = 0-4 places to mark (cities, battles, islands) with real coordinates. Names of stops and pins in {language}, max 3 words. Leave events empty, current 0.
- type "timeline": events = 3-6 dates in order from the narration of this scene or the story around it, label = the date as said ("1896", "XV век", "1 марта"), text = what happened, max 7 words, in {language}; current = the index of the event this scene is about. Leave countries, route and pins empty.
- title: a short plate in {language}, max 5 words ("Эфиопия, 1896", "Путь Магеллана").
Never invent facts: only places and dates the narration says or that are certain common knowledge.

SCENES:
{scenes}"""


def plan_prompt(scenes: list, language: str, total_seconds: float) -> str:
    limit = max(1, min(10, round(total_seconds / 100)))
    rows = "\n".join(f"{i}. {sc.get('text', '')}" for i, sc in enumerate(scenes))
    return PLAN_PROMPT.format(limit=limit, language=language, scenes=rows)


def clean_plan(items: list, n_scenes: int) -> list:
    """Drops what cannot be drawn: unknown places, too few dates, the first and the last scene, neighbours."""
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
        if g.get("type") == "map" and not map_ok(g):
            continue
        if g.get("type") == "timeline" and not timeline_ok(g):
            continue
        if g.get("type") not in KINDS:
            continue
        out.append({k: g.get(k) for k in ("index", "type", "title", "countries", "route", "pins", "events", "current")})
        used.add(i)
    return out
