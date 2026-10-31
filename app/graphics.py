"""Data animations over the video: counters, money, percents, timers, comparisons, ticks and crosses, arrows,
key words. The scriptwriter model marks the moments with important data; here each one is drawn frame by frame
(no paid service) and laid over the picture at the moment the voice says it."""
import math
import re
import shutil
from pathlib import Path

from PIL import Image, ImageDraw

from . import cta, media

FPS = 30
W, H = 900, 480          # the card canvas at 1080 px frame width
SS = 2
DUR = 3.0

WHITE = (255, 255, 255, 255)
CARD = (12, 14, 20, 200)
GREEN, RED, GOLD, BLUE, AMBER = (52, 211, 120, 255), (239, 68, 68, 255), (250, 204, 21, 255), (59, 130, 246, 255), (245, 158, 11, 255)
GREY = (120, 128, 140, 255)

# id: (Russian name for the owner, what the model must fill in)
TEMPLATES = {
    "counter": ("Счётчик", "value: the number; label: what it counts (\"человек\", \"раз\")"),
    "money": ("Деньги", "value: the amount; unit: currency sign ₽, $ or €; label: what the money is"),
    "percent": ("Процент в круге", "value: 0-100; label: what it is"),
    "bar": ("Полоса заполнения", "value: 0-100 percent; label: what fills up"),
    "timer": ("Таймер обратного отсчёта", "value: seconds to count down from (3-60); label: optional"),
    "stopwatch": ("Секундомер", "value: seconds it runs up to; label: optional"),
    "compare": ("Сравнение двух столбиков", "label_a, value_a, label_b, value_b: the two things and their numbers"),
    "check": ("Галочка: правда, можно", "label: the statement (2-5 words)"),
    "cross": ("Крестик: неправда, ошибка", "label: the statement (2-5 words)"),
    "ban": ("Знак запрета", "label: what is forbidden or must not be done (2-5 words)"),
    "up": ("Стрелка вверх: рост", "value: optional number (+300); unit: optional (%); label: what grows"),
    "down": ("Стрелка вниз: падение", "value: optional number; unit: optional; label: what falls"),
    "keyword": ("Ключевое слово", "label: the key word or 1-4 word phrase that must hit hard"),
    "date": ("Дата или год", "label: the date or year as written on screen (\"1941\", \"12 апреля\")"),
    "rank": ("Место в рейтинге: №1, топ", "value: the place number; label: what the ranking is"),
    "warning": ("Предупреждение «!»", "label: the warning (2-5 words)"),
}


def _ease(x):
    x = min(max(x, 0.0), 1.0)
    return 1 - (1 - x) ** 3


def _ease_back(x):
    return cta._ease_back(x)


def _font(px):
    return cta._font(int(px * SS))


def fmt_number(v: float, decimals: int = 0) -> str:
    s = f"{abs(v):,.{decimals}f}".replace(",", " ").replace(".", ",")
    return ("−" if v < 0 else "") + s


def _num(v, default=0.0) -> float:
    try:
        return float(str(v).replace(" ", "").replace(",", ".").replace("−", "-").lstrip("+"))
    except (TypeError, ValueError):
        return default


def _decimals(v) -> int:
    m = re.search(r"[.,](\d+)", str(v or ""))
    return min(len(m.group(1)), 2) if m else 0


def _fit(d: ImageDraw.ImageDraw, text: str, px: int, max_w: float):
    """The biggest font up to px that fits max_w."""
    while px > 18:
        f = _font(px)
        if d.textlength(text, font=f) <= max_w:
            return f
        px -= 4
    return _font(px)


def _finish(layer: Image.Image, t: float) -> Image.Image:
    scale = 0.4 + 0.6 * _ease_back(t / 0.32)
    out_t = DUR - 0.35
    alpha = min(1.0, t / 0.15)
    if t > out_t:
        k = _ease((t - out_t) / 0.35)
        scale *= 1 - 0.25 * k
        alpha *= 1 - k
    canvas = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    cta._place(canvas, layer, scale, alpha, W * SS / 2, H * SS / 2)
    return canvas.resize((W, H), Image.LANCZOS)


def _panel(w: float = 820, h: float = 400):
    im = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    x0, y0 = (W - w) / 2 * SS, (H - h) / 2 * SS
    d.rounded_rectangle([x0, y0, x0 + w * SS, y0 + h * SS], radius=36 * SS, fill=CARD)
    return im, d, (x0, y0, x0 + w * SS, y0 + h * SS)


def _label(d, box, text, y_frac=0.80, px=46, color=WHITE):
    if not text:
        return
    x0, y0, x1, y1 = box
    f = _fit(d, text, px, (x1 - x0) - 60 * SS)
    d.text(((x0 + x1) / 2, y0 + (y1 - y0) * y_frac), text, font=f, fill=color, anchor="mm")


def _counting(t: float, start: float = 0.15, length: float = 1.4) -> float:
    return _ease((t - start) / length)


def frame(g: dict, t: float) -> Image.Image:
    kind = g.get("type")
    label = (g.get("label") or "").strip()[:40]
    im, d, box = _panel()
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    if kind in ("counter", "money", "rank"):
        v = _num(g.get("value"))
        k = _counting(t)
        txt = fmt_number(v * k, _decimals(g.get("value")))
        if kind == "money":
            unit = (g.get("unit") or "₽").strip()[:3]
            txt = f"{unit}{txt}" if unit in ("$", "€") else f"{txt} {unit}"
        if kind == "rank":
            txt = f"№{max(1, round(v * k)) if k > 0.05 else ''}"
        color = GOLD if kind in ("money", "rank") else WHITE
        f = _fit(d, txt if k < 1 else txt, 150, (x1 - x0) - 80 * SS)
        pop = 1 + 0.08 * math.sin(min(1.0, max(0.0, (t - 1.55) / 0.25)) * math.pi)
        layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).text((cx, y0 + (y1 - y0) * 0.40), txt, font=f, fill=color, anchor="mm")
        if pop != 1:
            layer = layer.resize((int(layer.width * pop), int(layer.height * pop)), Image.LANCZOS)
            layer = layer.crop(((layer.width - im.width) // 2, (layer.height - im.height) // 2,
                                (layer.width + im.width) // 2, (layer.height + im.height) // 2))
        im.alpha_composite(layer)
        _label(d, box, label)
    elif kind == "percent":
        v = max(0.0, min(100.0, _num(g.get("value"))))
        k = _counting(t)
        r = 120 * SS
        ccx, ccy = cx, y0 + (y1 - y0) * 0.42
        d.ellipse([ccx - r, ccy - r, ccx + r, ccy + r], outline=(255, 255, 255, 60), width=22 * SS)
        if v * k > 0.2:
            d.arc([ccx - r, ccy - r, ccx + r, ccy + r], -90, -90 + 360 * v / 100 * k, fill=GREEN, width=22 * SS)
        d.text((ccx, ccy), f"{round(v * k)}%", font=_font(70), fill=WHITE, anchor="mm")
        _label(d, box, label, 0.88, 40)
    elif kind == "bar":
        v = max(0.0, min(100.0, _num(g.get("value"))))
        k = _counting(t)
        bx0, bx1, by = x0 + 70 * SS, x1 - 70 * SS, y0 + (y1 - y0) * 0.50
        hh = 46 * SS
        d.rounded_rectangle([bx0, by - hh / 2, bx1, by + hh / 2], radius=hh / 2, fill=(255, 255, 255, 50))
        fill_w = (bx1 - bx0) * v / 100 * k
        if fill_w > hh:
            d.rounded_rectangle([bx0, by - hh / 2, bx0 + fill_w, by + hh / 2], radius=hh / 2, fill=BLUE)
        d.text((cx, y0 + (y1 - y0) * 0.24), f"{round(v * k)}%", font=_font(84), fill=WHITE, anchor="mm")
        _label(d, box, label, 0.80, 42)
    elif kind in ("timer", "stopwatch"):
        total = max(1.0, min(_num(g.get("value"), 10), 3600))
        run = max(0.0, min(1.0, (t - 0.2) / (DUR - 0.8)))
        sec = total * (1 - run) if kind == "timer" else total * run
        m, s = divmod(int(math.ceil(sec) if kind == "timer" else int(sec)), 60)
        txt = f"{m:02d}:{s:02d}"
        ccx, ccy, r = x0 + (x1 - x0) * 0.26, y0 + (y1 - y0) * 0.46, 105 * SS
        d.ellipse([ccx - r, ccy - r, ccx + r, ccy + r], outline=WHITE, width=12 * SS)
        d.rectangle([ccx - 18 * SS, ccy - r - 34 * SS, ccx + 18 * SS, ccy - r - 8 * SS], fill=WHITE)
        frac = (1 - run) if kind == "timer" else run
        if frac > 0.01:
            d.pieslice([ccx - r + 22 * SS, ccy - r + 22 * SS, ccx + r - 22 * SS, ccy + r - 22 * SS], -90, -90 + 360 * frac,
                       fill=RED if kind == "timer" else BLUE)
        ang = math.radians(-90 + 360 * (run * 3 % 1))
        d.line([ccx, ccy, ccx + math.cos(ang) * (r - 30 * SS), ccy + math.sin(ang) * (r - 30 * SS)], fill=WHITE, width=8 * SS)
        d.text((x0 + (x1 - x0) * 0.68, ccy), txt, font=_font(110), fill=WHITE, anchor="mm")
        _label(d, box, label, 0.86, 38)
    elif kind == "compare":
        a, b = _num(g.get("value_a")), _num(g.get("value_b"))
        top = max(abs(a), abs(b), 1e-9)
        k = _counting(t, 0.2, 1.2)
        base, hmax = y0 + (y1 - y0) * 0.74, (y1 - y0) * 0.50
        for i, (lab, v, col) in enumerate(((g.get("label_a"), a, GREY), (g.get("label_b"), b, GOLD))):
            bx = x0 + (x1 - x0) * (0.30 if i == 0 else 0.70)
            bw = 150 * SS
            hgt = hmax * abs(v) / top * k
            if hgt > 4:
                d.rounded_rectangle([bx - bw / 2, base - hgt, bx + bw / 2, base], radius=14 * SS, fill=col)
            d.text((bx, base - hgt - 30 * SS), fmt_number(v * k, max(_decimals(g.get("value_a")), _decimals(g.get("value_b")))),
                   font=_font(50), fill=WHITE, anchor="mm")
            lab = str(lab or "")[:18]
            d.text((bx, base + 40 * SS), lab, font=_fit(d, lab, 40, (x1 - x0) * 0.42), fill=WHITE, anchor="mm")
    elif kind in ("check", "cross", "ban", "warning"):
        ccx, ccy, r = cx, y0 + (y1 - y0) * 0.38, 105 * SS
        k = _counting(t, 0.15, 0.5)
        col = {"check": GREEN, "cross": RED, "ban": RED, "warning": AMBER}[kind]
        if kind == "warning":
            pts = [(ccx, ccy - r), (ccx + r * 1.1, ccy + r * 0.85), (ccx - r * 1.1, ccy + r * 0.85)]
            d.polygon(pts, fill=col)
            if k > 0.3:
                d.text((ccx, ccy + r * 0.2), "!", font=_font(130), fill=(20, 20, 20, 255), anchor="mm")
        elif kind == "ban":
            d.ellipse([ccx - r, ccy - r, ccx + r, ccy + r], outline=col, width=24 * SS)
            e = r * 0.7 * k
            d.line([ccx - e, ccy + e, ccx + e, ccy - e], fill=col, width=24 * SS)
        else:
            d.ellipse([ccx - r, ccy - r, ccx + r, ccy + r], fill=col)
            if kind == "check":
                p1, p2, p3 = (ccx - r * .45, ccy + r * .02), (ccx - r * .1, ccy + r * .38), (ccx + r * .5, ccy - r * .35)
                if k > 0:
                    m = min(1.0, k * 2)
                    d.line([p1, (p1[0] + (p2[0] - p1[0]) * m, p1[1] + (p2[1] - p1[1]) * m)], fill=WHITE, width=22 * SS)
                if k > 0.5:
                    m = (k - 0.5) * 2
                    d.line([p2, (p2[0] + (p3[0] - p2[0]) * m, p2[1] + (p3[1] - p2[1]) * m)], fill=WHITE, width=22 * SS)
            else:
                e = r * 0.42 * k
                d.line([ccx - e, ccy - e, ccx + e, ccy + e], fill=WHITE, width=22 * SS)
                d.line([ccx - e, ccy + e, ccx + e, ccy - e], fill=WHITE, width=22 * SS)
        _label(d, box, label, 0.82, 46)
    elif kind in ("up", "down"):
        up = kind == "up"
        col = GREEN if up else RED
        k = _counting(t, 0.1, 0.9)
        ax = x0 + (x1 - x0) * 0.26
        top_y, bot_y = y0 + (y1 - y0) * 0.16, y0 + (y1 - y0) * 0.70
        # A zigzag line drawn from left to right, ending in an arrow.
        pts = [(x0 + 50 * SS, bot_y if up else top_y), (ax - 40 * SS, (top_y + bot_y) / 2 + (30 if up else -30) * SS),
               (ax, (top_y + bot_y) / 2 + (50 if up else -50) * SS), (ax + 90 * SS, top_y if up else bot_y)]
        n = max(1, int(k * 30))
        path = []
        seg = len(pts) - 1
        for i in range(n + 1):
            p = i / 30 * seg
            j = min(int(p), seg - 1)
            f = p - j
            path.append((pts[j][0] + (pts[j + 1][0] - pts[j][0]) * f, pts[j][1] + (pts[j + 1][1] - pts[j][1]) * f))
        if len(path) > 1:
            d.line(path, fill=col, width=18 * SS, joint="curve")
            ex, ey = path[-1]
            px, py = path[-2]
            ang = math.atan2(ey - py, ex - px)
            s = 46 * SS
            d.polygon([(ex + math.cos(ang) * s * .6, ey + math.sin(ang) * s * .6),
                       (ex + math.cos(ang + 2.4) * s, ey + math.sin(ang + 2.4) * s),
                       (ex + math.cos(ang - 2.4) * s, ey + math.sin(ang - 2.4) * s)], fill=col)
        val = str(g.get("value") or "").strip()
        if val:
            v = _num(val)
            txt = ("+" if up and not val.startswith(("-", "−")) else "−" if not up and v > 0 else "") \
                + fmt_number(v * _counting(t), _decimals(val)).lstrip("−") + (g.get("unit") or "")[:3]
            d.text((x0 + (x1 - x0) * 0.70, y0 + (y1 - y0) * 0.40), txt,
                   font=_fit(d, txt, 110, (x1 - x0) * 0.50), fill=col, anchor="mm")
        _label(d, box, label, 0.86, 42)
    elif kind == "date":
        pw, ph = 300 * SS, 240 * SS
        px0, py0 = cx - pw / 2, y0 + (y1 - y0) * 0.10
        d.rounded_rectangle([px0, py0, px0 + pw, py0 + ph], radius=24 * SS, fill=WHITE)
        d.rounded_rectangle([px0, py0, px0 + pw, py0 + 60 * SS], radius=24 * SS, fill=RED)
        d.rectangle([px0, py0 + 34 * SS, px0 + pw, py0 + 60 * SS], fill=RED)
        txt = label or str(g.get("value") or "")
        d.text((cx, py0 + ph * 0.62), txt, font=_fit(d, txt, 90, pw - 30 * SS), fill=(20, 20, 20, 255), anchor="mm")
        sub = (g.get("unit") or "").strip()[:30]
        if sub:
            _label(d, box, sub, 0.88, 40)
    else:  # keyword
        txt = (label or str(g.get("value") or "")).upper()
        lines = _wrap(d, txt, (x1 - x0) - 80 * SS, 110)
        f = lines[1]
        step = f.size * 1.1
        y = cy - step * (len(lines[0]) - 1) / 2
        for i, line in enumerate(lines[0]):
            shown = _counting(t, 0.1 + i * 0.12, 0.25)
            if shown > 0:
                d.text((cx, y + i * step + (1 - shown) * 30 * SS), line, font=f,
                       fill=(255, 255, 255, int(255 * shown)), anchor="mm")
        uy = min(y + (len(lines[0]) - 1) * step + step * 0.62, y1 - 22 * SS)
        u = 120 * SS * _counting(t, 0.3, 0.4)
        d.rectangle([cx - u, uy, cx + u, uy + 10 * SS], fill=GOLD)
    return _finish(im, t)


def _wrap(d, text: str, max_w: float, px: int):
    """Words in at most 3 lines with the biggest font that fits."""
    words = text.split()
    while px > 30:
        f = _font(px)
        lines, cur = [], ""
        for w in words:
            test = (cur + " " + w).strip()
            if d.textlength(test, font=f) <= max_w or not cur:
                cur = test
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
        if len(lines) <= 3 and all(d.textlength(x, font=f) <= max_w for x in lines):
            return lines, f
        px -= 6
    return [text[:30]], _font(30)


def _frames(g: dict, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(int(DUR * FPS)):
        frame(g, i / FPS).save(folder / f"f{i:03d}.png")
    return folder / "f%03d.png"


def overlay(video: Path, out: Path, items: list, width: int, height: int, work: Path, fps: int = 0) -> int:
    """items: [{type, label, value, ..., start}] in seconds. Draws them over the silent picture-only video."""
    items = [g for g in items if g.get("type") in TEMPLATES]
    if not items:
        return 0
    vertical = height > width
    k = (width / 1080) * (1.0 if vertical else 0.62)
    ow, oh = int(W * k) // 2 * 2, int(H * k) // 2 * 2
    # Vertical: above the subtitles and below the subscribe buttons. Horizontal: the top middle, away from subtitles.
    x, y = (width - ow) // 2, int(height * (0.33 if vertical else 0.06))
    args, fc, last = ["-i", video], [], "0:v"
    try:
        for i, g in enumerate(items):
            # Frames already drawn (an animation that spans two scene clips) are reused.
            args += ["-framerate", FPS, "-i", g.get("_frames") or _frames(g, work / f"gfx_{i:02d}")]
            fc.append(f"[{i + 1}:v]scale={ow}:{oh},format=rgba,setpts=PTS-STARTPTS+{g['start']:.3f}/TB[g{i}]")
            fc.append(f"[{last}][g{i}]overlay={x}:{y}:eof_action=pass:format=auto[v{i}]")
            last = f"v{i}"
        rate = ["-r", fps] if fps else []  # a scene clip keeps its frame rate, so it joins with the others
        media.run([*args, "-filter_complex", ";".join(fc), "-map", f"[{last}]", *rate, "-c:v", "libx264",
                   "-preset", "veryfast", "-crf", 18, "-pix_fmt", "yuv420p", "-an", out], "Анимации данных")
    finally:
        for i in range(len(items)):
            shutil.rmtree(work / f"gfx_{i:02d}", ignore_errors=True)
    return len(items)


def _norm(w: str) -> str:
    return re.sub(r"[\W_]", "", w.lower().replace("ё", "е"))


def place(plan: list, words: list, durations: list, total: float, vertical: bool) -> list:
    """Start times: the moment the trigger word is spoken in its scene; overlapping or too close ones are dropped."""
    starts = [sum(durations[:i]) for i in range(len(durations))]
    gap = 4.0 if vertical else 12.0
    out, last = [], -99.0
    for g in sorted(plan, key=lambda g: int(g.get("index", 0))):
        i = int(g.get("index", -1))
        if not 0 <= i < len(durations):
            continue
        trig = _norm(str(g.get("trigger") or ""))[:6]
        t = starts[i] + 0.2
        if trig:
            hit = next((w for w in words if w[3] == i and _norm(w[0]).startswith(trig[:max(3, len(trig))])), None)
            if hit:
                t = max(starts[i], hit[1] - 0.15)
        if t - last < gap or t + DUR > total - 0.3:
            continue
        out.append({**g, "start": round(t, 2)})
        last = t
    return out


PLAN_PROMPT = """You choose on-screen data animations for a narrated video. Each animation appears for 3 seconds over the picture at the moment the voice says the fact, to confirm it visually: a counter for a number, money, a percent, a timer, a comparison, a tick, a cross, a ban sign, an arrow, a key word, a date, a rank, a warning.

TEMPLATES (type: what to fill in):
{templates}

RULES:
- Only real data or a strong key point of the narration: numbers, money, percents, time, years, comparisons, "true/false", "never do this", growth or fall. Never invent a number that the narration does not say.
- {limit}
- Prefer a variety of types; never the same type twice in a row.
- Texts (label, unit, label_a, label_b) are short, in {lang}, the way they are written on screen (2-5 words, max 24 characters). Values are plain numbers without spaces ("2000000", "37.5").
- index: the scene number; trigger: the exact word of that scene's narration when the animation must appear (usually the number itself or the key word).
- Return an empty list if the video has no such moments.

SCENES:
{scenes}"""


def plan_prompt(scenes: list, vertical: bool, lang: str) -> str:
    total_words = sum(len(s.get("text", "").split()) for s in scenes)
    if vertical:
        limit = "At most 3 animations, and not in the first sentence (the hook must be heard first)."
    else:
        limit = f"At most {max(2, min(12, total_words // 90))} animations, spread over the video, at least 20 seconds apart."
    return PLAN_PROMPT.format(
        templates="\n".join(f"- {k}: {v[1]}" for k, v in TEMPLATES.items()), limit=limit,
        lang={"ru": "Russian", "de": "German", "es": "Spanish", "fr": "French"}.get(lang, "English"),
        scenes="\n".join(f"{i}. {s.get('text', '')}" for i, s in enumerate(scenes)))


SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
    "index": {"type": "integer"}, "type": {"type": "string", "enum": list(TEMPLATES)}, "trigger": {"type": "string"},
    "value": {"type": "string"}, "unit": {"type": "string"}, "label": {"type": "string"},
    "label_a": {"type": "string"}, "value_a": {"type": "string"}, "label_b": {"type": "string"},
    "value_b": {"type": "string"}}}}}}
