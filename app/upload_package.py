"""What goes to YouTube with a video built from a project folder: the owner's upload file
<LANG>/загрузка_YouTube_<LANG>.md (1. Название, 2. Превью, 3. Описание, 4. Теги, 5. Категория) and the thumbnail.
In the flat layout the same files lie in the project folder itself, the language only in the name
(загрузка_YouTube_ES.md, превью_ES.txt).

The new thumbnail file превью_<LANG>.txt («1. ПРОМТ КАРТИНКИ БЕЗ ТЕКСТА», «2. ТЕКСТ СЛЕВА» with a colour per line,
«3. ПЛАШКА») gives its own picture (drawn once per language, исходники/превью_основа_<LANG>.png) and its lines are
laid on the black left part in their colours, the plate red at the bottom left.
Since 214 the file may say «РАСКЛАДКА: A–H» (see compose_layout): B wall on the right, C dark top, D dark bottom
with a ribbon, E cinema still, F object in the centre with words in two corners, G split screen, H stamps;
without that line it is A, the old black wall on the left. A line may add its place («…, сверху слева»), and the
plate its colours («Жёлтый прямоугольник…, чёрные буквы»).

The thumbnail: a finished <LANG>/превью*.png|jpg is used as it is. Otherwise the picture without text is drawn once
for all languages from the prompt in section 2 (the English file holds it; other languages say «картинка из EN») and
saved as исходники/превью_основа.png; then each language gets its own words laid on it: the plain line in black,
the key word white on a red plate, top left, the bottom right corner left empty for the timer.
"""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .net import UserError

PIC_EXT = (".png", ".jpg", ".jpeg", ".webp")
RED, BLACK, WHITE = (220, 32, 38), (12, 12, 12), (255, 255, 255)
YELLOW = (255, 212, 0)
LAYOUTS = "ABCDEFGH"
FONTS = ["/System/Library/Fonts/Supplemental/Arial Black.ttf", "/System/Library/Fonts/Supplemental/Impact.ttf",
         "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
CONDENSED = ["/System/Library/Fonts/Supplemental/Impact.ttf", "/System/Library/Fonts/Supplemental/Arial Narrow Bold.ttf",
             "/System/Library/Fonts/Supplemental/Arial Black.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
LANG_CODES = ("EN", "RU", "DE", "ES")
COLORS = [(r"бел|white", WHITE), (r"ж[её]лт|yellow", YELLOW), (r"з[её]л[её]н|green", (46, 204, 64)),
          (r"красн|red", RED), (r"ч[её]рн|black", BLACK), (r"оранж|orange", (255, 140, 0)),
          (r"голуб|син|blue", (64, 160, 255))]


def _has_code(stem: str, code: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z]){code}(?![A-Za-z])", stem))


def _for_lang(files: list[Path], lang: str | None) -> list[Path]:
    """In a folder shared by several languages: the files named for this language, else the ones named for none."""
    if not lang:
        return files
    code = lang.upper()
    own = [p for p in files if _has_code(p.stem, code)]
    return own or [p for p in files if not any(_has_code(p.stem, c) for c in LANG_CODES)]


def find_file(lang_dir: Path, lang: str | None = None) -> Path | None:
    files = [p for p in lang_dir.glob("*.md") if ("загруз" in p.name.lower() or "upload" in p.name.lower())
             and not p.name.startswith(".")]
    files = _for_lang(files, lang)
    return min(files, key=lambda p: len(p.name)) if files else None


def find_thumb_spec(lang_dir: Path, lang: str) -> Path | None:
    """превью_<LANG>.txt: the thumbnail's own file (picture prompt, lines with colours, plate)."""
    files = [p for p in lang_dir.glob("*.txt") if re.search(r"(?i)превью|thumb", p.stem) and not p.name.startswith(".")]
    files = _for_lang(files, lang)
    return min(files, key=lambda p: len(p.name)) if files else None


def _color(spec: str):
    for pattern, rgb in COLORS:
        if re.search(pattern, spec, re.I):
            return rgb
    return None


def parse_thumb_spec(path: Path) -> dict:
    """{prompt, lines: [[(text, rgb), …] per line], plate} from превью_<LANG>.txt."""
    text = path.read_text("utf-8", errors="replace")
    heads = list(re.finditer(r"(?m)^\s*(\d)\.\s+([^\n]+)$", text))
    secs = {}
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        secs[m.group(2).strip().lower()] = text[m.end():end].strip()

    def sec(*words, start=False):
        return next((body for head, body in secs.items()
                     if (head.startswith(words[0]) if start else all(w in head for w in words))), "")

    prompt = " ".join(x.strip() for x in (sec("промт", "без") or sec("промпт", "без") or sec("промт") or "").splitlines()
                      if x.strip())
    lines, line_pos = [], []
    for raw in (sec("текст", start=True) or sec("строк", start=True)).splitlines():
        if not raw.strip() or "—" not in raw and " - " not in raw:
            continue
        words, _, spec = raw.rpartition("—") if "—" in raw else raw.rpartition(" - ")
        words = words.strip()
        if not words:
            continue
        base = _color(spec.split(",")[0]) or WHITE
        parts = [(words, base)]
        for phrase, how in re.findall(r"«([^»]+)»\s*([^,«]+)", spec):
            rgb = _color(how)
            i = words.upper().find(phrase.upper())
            if rgb and i >= 0:
                parts = [(words[:i], base), (words[i:i + len(phrase)], rgb), (words[i + len(phrase):], base)]
                parts = [x for x in parts if x[0]]
                break
        lines.append(parts)
        line_pos.append(_position(spec))
    plate, plate_bg, plate_fg = "", RED, WHITE
    for raw in sec("плашк", start=True).splitlines():
        raw = raw.strip()
        if raw:
            plate = raw.rsplit(":", 1)[-1].strip(" «»\"") if ":" in raw else raw
            how = raw.rsplit(":", 1)[0] if ":" in raw else ""
            found = [(m.start(), rgb) for pattern, rgb in COLORS for m in [re.search(pattern, how, re.I)] if m]
            found.sort()
            if found:
                plate_bg = found[0][1]
                plate_fg = found[1][1] if len(found) > 1 else (BLACK if plate_bg in (WHITE, YELLOW) else WHITE)
            break
    m = re.search(r"(?mi)^\s*(?:раскладка|layout)\s*:\s*([A-Za-zА-Яа-я])", text)
    layout = (m.group(1).upper().translate(str.maketrans("АВСЕНВ", "ABCEHB")) if m else "A")
    if layout not in LAYOUTS:
        layout = "A"
    return {"prompt": prompt, "lines": lines, "line_pos": line_pos, "plate": plate, "plate_bg": plate_bg,
            "plate_fg": plate_fg, "layout": layout, "file": str(path)}


def _position(spec: str) -> str:
    """«…, сверху слева» → "top-left"; only the parts that are said."""
    s = spec.lower()
    v = "top" if re.search(r"сверх|top|вверх", s) else "bottom" if re.search(r"сниз|bottom|вниз", s) else ""
    h = "left" if re.search(r"слев|left", s) else "right" if re.search(r"справ|right", s) else ""
    return "-".join(x for x in (v, h) if x)


def _sections(text: str) -> dict[str, str]:
    out = {}
    parts = re.split(r"(?m)^##\s+", text)
    for part in parts[1:]:
        head, _, body = part.partition("\n")
        out[head.strip().lower()] = body.strip()
    return out


def _section(secs: dict, *names: str) -> str:
    for head, body in secs.items():
        if any(n in head for n in names):
            return body
    return ""


def _plain(s: str) -> str:
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    return s.strip()


def parse(path: Path) -> dict:
    """{title, description, tags[], category, kids, thumb_prompt, thumb_lines[(text, plate)]}"""
    text = path.read_text("utf-8", errors="replace")
    secs = _sections(text)
    title = next((_plain(l).strip() for l in _section(secs, "назван", "title").splitlines() if l.strip()), "")
    # Quotes around the whole title go; a quote that only opens the title («…», escribió…) stays.
    if len(title) > 1 and title[0] + title[-1] in ("«»", '""') and title.count("»") + title.count('"') <= 2:
        title = title[1:-1].strip()
    description = _plain(_section(secs, "описан", "description"))
    description = re.sub(r"\n{3,}", "\n\n", description).strip()
    tags_raw = " ".join(_section(secs, "теги", "tags").split("\n")).strip()
    tags = [t.strip(" .#") for t in tags_raw.split(",") if t.strip(" .#")]
    category = _plain(_section(secs, "категор", "category").split("\n")[0]) if _section(secs, "категор", "category") else ""
    kids = bool(re.search(r"(?i)для детей|made for kids", category)) and not re.search(r"(?i)не для детей|not made for kids", category)
    cat_all = _section(secs, "категор", "category")
    # «Изменённый или синтетический контент»: да/нет (the AI voice); None = the file says nothing, Studio is left as is.
    m = re.search(r"(?i)(?:синтетическ|altered|synthetic)[^\n:]*[»\"]?\s*[:—-]\s*\**\s*(да|нет|yes|no)", cat_all)
    altered = None if not m else m.group(1).lower() in ("да", "yes")
    thumb = _section(secs, "превью", "thumbnail")
    main = re.split(r"(?i)\*\*\s*запасн|\*\*\s*alternative", thumb)[0]
    prompt = ""
    m = re.search(r"(?ms)^\s*>\s?(.+?)(?:\n\s*\n|\Z)", main)
    if m:
        prompt = " ".join(x.strip().lstrip(">").strip() for x in m.group(1).splitlines()).strip()
    lines = []
    t = re.search(r"(?is)текст[^:\n]*:\**(.*?)(?:\n\s*-\s*\*\*|\n\s*\n|\Z)", main)
    if t:
        for word, how in re.findall(r"([^\n—;,*]+?)\s+—\s+(чёрн\w*|черн\w*|бел\w*(?:\s+в\s+красн\w*\s+плашк\w*)?|красн\w*)",
                                    t.group(1)):
            word = word.strip(" -–«»\"").strip()
            if word:
                lines.append((word, "плашк" in how or how.startswith("красн")))
    wpm = re.search(r"(\d{2,3})\s+(?:словах|слов)\s+в\s+минуту", text)
    return {"title": title[:100], "description": description[:5000], "tags": tags, "category": category, "kids": kids,
            "altered": altered, "thumb_prompt": prompt, "thumb_lines": lines, "file": str(path),
            "chapters": chapters(description), "wpm": int(wpm.group(1)) if wpm else 150}


TIMECODE = re.compile(r"(?m)^[ \t]*((?:\d{1,2}:)?\d{1,2}:\d{2})[ \t]+(?:[-–—][ \t]*)?(\S[^\n]*)$")


def _seconds(code: str) -> int:
    total = 0
    for part in code.split(":"):
        total = total * 60 + int(part)
    return total


def timecode(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def chapters(description: str) -> list[tuple[int, str]]:
    """The «0:00 Title» lines of the description, in order."""
    return [(_seconds(m.group(1)), m.group(2).strip()) for m in TIMECODE.finditer(description)]


def retime_description(description: str, times: list[float]) -> str:
    """The same «0:00 Title» lines with the times measured on the real voice-over."""
    found = list(TIMECODE.finditer(description))
    if len(found) != len(times):
        return description
    out, last = [], 0
    for m, t in zip(found, times):
        out.append(description[last:m.start(1)] + timecode(t))
        last = m.end(1)
    return "".join(out) + description[last:]


def _font(size: int, text: str):
    for p in FONTS:
        if Path(p).exists():
            try:
                f = ImageFont.truetype(p, size)
                if all(f.getmask(ch).getbbox() for ch in text if not ch.isspace() and ch.isalpha()):
                    return f
            except OSError:
                continue
    return ImageFont.load_default()


def compose(base: Path, out: Path, lines: list):
    """The words on the picture, top left: black text with a thin white edge, the key word white on a red plate."""
    im = Image.open(base).convert("RGB")
    w, h = 1920, 1080
    sw, sh = im.size
    scale = max(w / sw, h / sh)
    im = im.resize((round(sw * scale), round(sh * scale)), Image.LANCZOS)
    left, top = (im.width - w) // 2, (im.height - h) // 2
    im = im.crop((left, top, left + w, top + h))
    d = ImageDraw.Draw(im)
    x, y = 70, 60
    max_w = int(w * .52)
    for text, plate in lines[:3]:
        size = 190
        while size > 60:
            f = _font(size, text)
            box = d.textbbox((0, 0), text, font=f)
            if box[2] - box[0] + (60 if plate else 0) <= max_w:
                break
            size -= 6
        box = d.textbbox((0, 0), text, font=f)
        tw, th = box[2] - box[0], box[3] - box[1]
        if plate:
            pad_x, pad_y = 30, 18
            d.rounded_rectangle([x, y, x + tw + 2 * pad_x, y + th + 2 * pad_y], radius=16, fill=RED)
            d.text((x + pad_x - box[0], y + pad_y - box[1]), text, font=f, fill=WHITE)
            y += th + 2 * pad_y + 22
        else:
            d.text((x - box[0], y - box[1]), text, font=f, fill=BLACK, stroke_width=max(3, size // 40), stroke_fill=WHITE)
            y += th + 26
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, "PNG", optimize=True)
    # YouTube takes thumbnails up to 2 MB.
    if out.stat().st_size > 2_000_000:
        jpg = out.with_suffix(".jpg")
        im.save(jpg, "JPEG", quality=90)
        out.unlink()
        return jpg
    return out


def finished_thumb(lang_dir: Path, lang: str | None = None) -> Path | None:
    pics = [p for p in lang_dir.iterdir() if p.suffix.lower() in PIC_EXT and not p.name.startswith(".")
            and re.search(r"(?i)превью|thumb", p.stem) and "основа" not in p.stem.lower()] if lang_dir.is_dir() else []
    pics = _for_lang(pics, lang)
    return min(pics, key=lambda p: len(p.name)) if pics else None


def _cover(im: Image.Image, w: int, h: int) -> Image.Image:
    sw, sh = im.size
    scale = max(w / sw, h / sh)
    im = im.resize((round(sw * scale), round(sh * scale)), Image.LANCZOS)
    left, top = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((left, top, left + w, top + h))


def _condensed(size: int, text: str):
    for p in CONDENSED:
        if Path(p).exists():
            try:
                f = ImageFont.truetype(p, size)
                if all(f.getmask(ch).getbbox() for ch in text if not ch.isspace() and ch.isalpha()):
                    return f
            except OSError:
                continue
    return _font(size, text)


def _save(im: Image.Image, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, "PNG", optimize=True)
    # YouTube takes thumbnails up to 2 MB.
    if out.stat().st_size > 2_000_000:
        jpg = out.with_suffix(".jpg")
        im.save(jpg, "JPEG", quality=90)
        out.unlink()
        return jpg
    return out


def compose_left(base: Path, out: Path, spec: dict) -> Path:
    """The превью_<LANG>.txt layout: picture on the right 40 %, black left 60 % with the lines in their colours,
    the plate red with white words at the bottom left."""
    w, h = 1920, 1080
    im = _cover(Image.open(base).convert("RGB"), w, h)
    # The picture is asked with an empty black left part; a soft black band keeps the words readable anyway.
    shade = Image.new("L", (w, h), 0)
    sd = ImageDraw.Draw(shade)
    solid, fade = int(w * .55), int(w * .63)
    sd.rectangle([0, 0, solid, h], fill=255)
    for x in range(solid, fade):
        sd.line([(x, 0), (x, h)], fill=round(255 * (fade - x) / (fade - solid)))
    im.paste(Image.new("RGB", (w, h), (0, 0, 0)), (0, 0), shade)
    d = ImageDraw.Draw(im)
    x0, top, max_w = 64, 56, solid - 64 - 30
    lines = [parts for parts in spec.get("lines") or [] if parts][:8]
    plate = (spec.get("plate") or "").strip()
    plate_h = 0
    if plate:
        size = 96
        while size > 36:
            pf = _condensed(size, plate)
            box = d.textbbox((0, 0), plate, font=pf)
            if box[2] - box[0] + 60 <= max_w:
                break
            size -= 4
        box = d.textbbox((0, 0), plate, font=pf)
        pw, ph = box[2] - box[0] + 60, box[3] - box[1] + 40
        py = h - 56 - ph
        d.rectangle([x0, py, x0 + pw, py + ph], fill=spec.get("plate_bg") or RED)
        d.text((x0 + 30 - box[0], py + 20 - box[1]), plate, font=pf, fill=spec.get("plate_fg") or WHITE)
        plate_h = ph + 40
    if lines:
        room = h - top - 56 - plate_h
        all_text = "".join(t for parts in lines for t, _ in parts)
        size = 150
        while size > 30:
            f = _condensed(size, all_text)
            widths = [sum(d.textlength(t, font=f) for t, _ in parts) for parts in lines]
            asc, desc = f.getmetrics()
            step = asc + desc + size // 8
            if max(widths) <= max_w and step * len(lines) <= room:
                break
            size -= 3
        asc, desc = f.getmetrics()
        step = asc + desc + size // 8
        y = top
        for parts in lines:
            x = x0
            for t, rgb in parts:
                d.text((x, y), t, font=f, fill=rgb)
                x += d.textlength(t, font=f)
            y += step
    return _save(im, out)


# ---------------------------------------------------------------- other thumbnail layouts (B–H)
# The layouts are described for the scriptwriter in drama-studio, references/10_превью_раскладки.md.
W, H = 1920, 1080


def _shade(im: Image.Image, mask: Image.Image, color=(0, 0, 0)) -> None:
    im.paste(Image.new("RGB", im.size, color), (0, 0), mask)


def _band(direction: str, solid: float, fade: float, strength: int = 255) -> Image.Image:
    """A dark band from one edge: fully dark up to `solid` (share of the side), fading out up to `fade`."""
    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)
    size = W if direction in ("left", "right") else H
    a, b = int(size * solid), int(size * fade)
    for i in range(b):
        v = strength if i <= a else round(strength * (b - i) / max(1, b - a))
        if direction == "left":
            d.line([(i, 0), (i, H)], fill=v)
        elif direction == "right":
            d.line([(W - 1 - i, 0), (W - 1 - i, H)], fill=v)
        elif direction == "top":
            d.line([(0, i), (W, i)], fill=v)
        else:
            d.line([(0, H - 1 - i), (W, H - 1 - i)], fill=v)
    return m


def _corner(cx: int, cy: int, radius: int, strength: int = 235) -> Image.Image:
    """A soft dark blot around a corner, so words there stay readable on any picture."""
    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)
    for r in range(radius, 0, -6):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=round(strength * (1 - r / radius) ** .6))
    return m


def _vignette(strength: int = 200) -> Image.Image:
    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)
    steps = 60
    for i in range(steps):
        k = i / steps
        d.rectangle([int(W * k * .32), int(H * k * .32), W - int(W * k * .32), H - int(H * k * .32)],
                    outline=round(strength * (1 - k) ** 2), width=int(W * .32 / steps) + 2)
    return m


def _fit(d: ImageDraw.ImageDraw, lines: list, max_w: int, room: int, start: int = 160):
    all_text = "".join(t for parts in lines for t, _ in parts) or "A"
    size = start
    while size > 28:
        f = _condensed(size, all_text)
        widths = [sum(d.textlength(t, font=f) for t, _ in parts) for parts in lines] or [0]
        asc, desc = f.getmetrics()
        step = asc + desc + size // 8
        if max(widths) <= max_w and step * len(lines) <= room:
            return f, step
        size -= 3
    f = _condensed(size, all_text)
    asc, desc = f.getmetrics()
    return f, asc + desc + size // 8


def _lines(d, lines: list, f, step: int, x: int, y: int, align: str = "left", width: int = 0,
           frame: tuple | None = None) -> int:
    """Draws the lines from (x, y); align left/right/center inside `width`. Returns the y under the last line."""
    for parts in lines:
        lw = sum(d.textlength(t, font=f) for t, _ in parts)
        lx = x if align == "left" else x + width - lw if align == "right" else x + (width - lw) / 2
        if frame:
            asc, desc = f.getmetrics()
            pad = max(10, step // 10)
            d.rectangle([lx - pad * 2, y - pad, lx + lw + pad * 2, y + asc + desc + pad], outline=frame,
                        width=max(4, step // 22))
        for t, rgb in parts:
            d.text((lx, y), t, font=f, fill=rgb, stroke_width=max(2, step // 45), stroke_fill=BLACK)
            lx += d.textlength(t, font=f)
        y += step
    return y


def _plate_box(d, text: str, max_w: int, size: int = 92):
    while size > 32:
        pf = _condensed(size, text)
        box = d.textbbox((0, 0), text, font=pf)
        if box[2] - box[0] + 60 <= max_w:
            break
        size -= 4
    box = d.textbbox((0, 0), text, font=pf)
    return pf, box, box[2] - box[0] + 60, box[3] - box[1] + 40


def _plate(d, spec: dict, x: int, y: int, anchor: str, max_w: int, size: int = 92):
    """The plate with its corner at (x, y): anchor lt / rt / lb / rb / cb (centre bottom) / ct. Returns its box."""
    text = (spec.get("plate") or "").strip()
    if not text:
        return None
    pf, box, pw, ph = _plate_box(d, text, max_w, size)
    left = x if anchor[0] == "l" else x - pw if anchor[0] == "r" else x - pw // 2
    top = y if anchor[1] == "t" else y - ph
    d.rectangle([left, top, left + pw, top + ph], fill=spec.get("plate_bg") or RED)
    d.text((left + 30 - box[0], top + 20 - box[1]), text, font=pf, fill=spec.get("plate_fg") or WHITE)
    return (left, top, left + pw, top + ph)


def _ribbon(im: Image.Image, spec: dict, corner: str = "rt", stamp: bool = False) -> None:
    """The plate as a diagonal ribbon (or a stamp frame) across a top corner."""
    text = (spec.get("plate") or "").strip()
    if not text:
        return
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    pf, box, pw, ph = _plate_box(d, text, int(W * .42), 84)
    x, y = (W - pw) // 2, (H - ph) // 2
    bg, fg = spec.get("plate_bg") or RED, spec.get("plate_fg") or WHITE
    if stamp:
        d.rectangle([x, y, x + pw, y + ph], outline=bg + (255,), width=10)
        d.text((x + 30 - box[0], y + 20 - box[1]), text, font=pf, fill=bg + (255,))
    else:
        d.rectangle([x - 400, y, x + pw + 400, y + ph], fill=bg + (255,))
        d.text((x + 30 - box[0], y + 20 - box[1]), text, font=pf, fill=fg + (255,))
    layer = layer.rotate(-14 if corner == "rt" else 14, resample=Image.BICUBIC, center=(W / 2, H / 2))
    dx = int(W * .33) if corner == "rt" else -int(W * .33)
    shifted = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    shifted.paste(layer, (dx, -int(H * .33)), layer)
    im.paste(shifted, (0, 0), shifted)


def _split(spec: dict, want: tuple[str, str]) -> tuple[list, list]:
    """Lines into two groups by their position words (want = the two keys); unsaid ones half and half."""
    lines, pos = spec.get("lines") or [], spec.get("line_pos") or []
    a, b, free = [], [], []
    for i, parts in enumerate(lines):
        p = pos[i] if i < len(pos) else ""
        if p and want[0] in p:
            a.append(parts)
        elif p and want[1] in p:
            b.append(parts)
        else:
            free.append(parts)
    half = (len(free) + 1) // 2
    return a + free[:half], b + free[half:]


def compose_layout(base: Path, out: Path, spec: dict) -> Path:
    """The thumbnail in the layout the file asks for (РАСКЛАДКА: A–H); A is the old black wall on the left."""
    layout = spec.get("layout") or "A"
    if layout == "A":
        return compose_left(base, out, spec)
    im = _cover(Image.open(base).convert("RGB"), W, H)
    lines = [parts for parts in spec.get("lines") or [] if parts][:8]
    m = 60
    if layout == "B":  # black wall on the right, text right-aligned, plate bottom right
        _shade(im, _band("right", .55, .63))
        d = ImageDraw.Draw(im)
        max_w = int(W * .55) - m - 30
        pbox = _plate(d, spec, W - m, H - 56, "rb", max_w)
        room = (pbox[1] if pbox else H - 56) - 56 - 30
        f, step = _fit(d, lines, max_w, room)
        _lines(d, lines, f, step, W - m - max_w, 56, "right", max_w)
    elif layout == "C":  # dark sky on top, big centred lines, plate bottom right
        d = ImageDraw.Draw(im)
        f, step = _fit(d, lines[:4], W - 2 * m, int(H * .44), 170)
        solid = (40 + step * len(lines[:4]) + 20) / H
        _shade(im, _band("top", solid, solid + .14))
        d = ImageDraw.Draw(im)
        _lines(d, lines[:4], f, step, m, 40, "center", W - 2 * m)
        _shade(im, _corner(W, H, 520, 200))
        d = ImageDraw.Draw(im)
        _plate(d, spec, W - m, H - 50, "rb", int(W * .45))
    elif layout == "D":  # full picture, dark bottom, big lines at the bottom, ribbon in the top right corner
        d = ImageDraw.Draw(im)
        f, step = _fit(d, lines[:4], W - 2 * m, int(H * .40), 170)
        solid = (40 + step * len(lines[:4]) + 20) / H
        _shade(im, _band("bottom", solid, solid + .14))
        d = ImageDraw.Draw(im)
        y = H - 40 - step * len(lines[:4])
        _lines(d, lines[:4], f, step, m, y, "center", W - 2 * m)
        _ribbon(im, spec, "rt")
    elif layout == "E":  # cinema still: vignette, one or two huge lines low in the centre, small plate above
        _shade(im, _vignette(190))
        _shade(im, _band("bottom", .14, .42, 230))
        d = ImageDraw.Draw(im)
        f, step = _fit(d, lines[:2], W - 2 * m, int(H * .34), 200)
        y = H - 50 - step * len(lines[:2])
        pbox = _plate(d, spec, W // 2, y - 20, "cb", int(W * .5), 64)
        _lines(d, lines[:2], f, step, m, y, "center", W - 2 * m)
    elif layout == "F":  # object in the centre, words in the top-left and bottom-right corners, plate bottom centre
        top, bottom = _split(spec, ("top", "bottom"))
        _shade(im, _corner(0, 0, 900))
        _shade(im, _corner(W, H, 900))
        d = ImageDraw.Draw(im)
        max_w = int(W * .56)
        f, step = _fit(d, top + bottom, max_w, int(H * .36) * len(top + bottom) // max(1, max(len(top), len(bottom))), 130)
        _lines(d, top, f, step, m, 46, "left", max_w)
        pbox = _plate(d, spec, W // 2, H - 40, "cb", int(W * .4), 76)
        yb = (pbox[1] - 20 if pbox else H - 50) - step * len(bottom)
        _lines(d, bottom, f, step, W - m - max_w, yb, "right", max_w)
    elif layout == "G":  # split screen: a light seam in the middle, words over each half, plate on the seam
        left, right = _split(spec, ("left", "right"))
        _shade(im, _band("top", .22, .40))
        d = ImageDraw.Draw(im)
        d.rectangle([W // 2 - 4, 0, W // 2 + 4, H], fill=(245, 245, 245))
        half = W // 2 - 2 * m
        f, step = _fit(d, left + right, half, int(H * .30) * len(left + right) // max(1, max(len(left), len(right))), 120)
        _lines(d, left, f, step, m, 40, "center", half)
        _lines(d, right, f, step, W // 2 + m, 40, "center", half)
        _plate(d, spec, W // 2, H - 50, "cb", int(W * .5), 80)
    elif layout == "H":  # the document: dimmed picture, lines in red frames like stamps, plate as a slanted stamp
        _shade(im, Image.new("L", (W, H), 120))
        d = ImageDraw.Draw(im)
        f, step = _fit(d, lines[:4], int(W * .70), int(H * .62), 120)
        step = int(step * 1.25)
        y = (H - step * len(lines[:4])) // 2 + 40
        _lines(d, lines[:4], f, step, int(W * .15), y, "center", int(W * .70), frame=spec.get("plate_bg") or RED)
        _ribbon(im, spec, "rt", stamp=True)
    return _save(im, out)


def _draw_base(settings: dict, prompt: str, base: Path):
    from . import images, media
    s = dict(settings)
    if s.get("image_provider") == "fastgen":
        s["fastgen_model"] = "nano_banana_pro_image_generate"
    media.to_png(images.generate(s, prompt, False), base)


def make_thumb(root: Path, lang: str, lang_dir: Path, package: dict, settings: dict, english: dict | None) -> Path:
    """The thumbnail of one language (see the module text). Returns its path."""
    done = finished_thumb(lang_dir, lang)
    if done:
        return done
    lines = package.get("thumb_lines") or []
    src_dir = next((p for p in root.iterdir() if p.is_dir() and p.name.lower() == "исходники"), root)
    spec_file = find_thumb_spec(lang_dir, lang) or (find_thumb_spec(src_dir, lang) if src_dir != lang_dir else None)
    if spec_file:
        spec = parse_thumb_spec(spec_file)
        code = lang.upper()
        base = next((p for p in src_dir.glob(f"превью_основа_{code}.*") if p.suffix.lower() in PIC_EXT), None)
        if base is None:
            if not spec["prompt"]:
                raise UserError(f"В файле {spec_file.name} нет промпта картинки (раздел «1. ПРОМТ КАРТИНКИ БЕЗ ТЕКСТА»).")
            base = src_dir / f"превью_основа_{code}.png"
            _draw_base(settings, spec["prompt"], base)
        return compose_layout(base, lang_dir / f"превью_{code}.png", spec)
    base = next((p for p in src_dir.glob("превью_основа.*") if p.suffix.lower() in PIC_EXT), None)
    if base is None:
        prompt = package.get("thumb_prompt") or (english or {}).get("thumb_prompt") or ""
        if not prompt:
            raise UserError(f"Для превью {lang.upper()} нет ни готовой картинки (превью_{lang.upper()}.png в папке "
                            f"{lang.upper()}), ни промпта в разделе «2. Превью» файла загрузки.")
        from . import images
        s = dict(settings)
        if s.get("image_provider") == "fastgen":
            s["fastgen_model"] = "nano_banana_pro_image_generate"
        raw = images.generate(s, prompt, False)
        base = src_dir / "превью_основа.png"
        from . import media
        media.to_png(raw, base)
    return compose(base, lang_dir / f"превью_{lang.upper()}.png", lines)
