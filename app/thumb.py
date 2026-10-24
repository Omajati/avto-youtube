"""YouTube thumbnail with a big headline, and a text file with everything to paste into YouTube."""
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONTS = [
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


SUP = "/System/Library/Fonts/Supplemental/"
# Headline fonts the writer can choose from: key -> (Russian name for the owner, [(file, style words in its name)]).
STYLE_FONTS = {
    "impact": ("Impact: высокий жирный, классика YouTube", [(SUP + "Impact.ttf", "")]),
    "black": ("Arial Black: широкий тяжёлый", [(SUP + "Arial Black.ttf", "")]),
    "condensed": ("Узкий современный", [("/System/Library/Fonts/Avenir Next Condensed.ttc", "Heavy"),
                                         (SUP + "Futura.ttc", "Condensed ExtraBold")]),
    "comic": ("Комикс: весёлый, для юмора и детей", [(SUP + "Comic Sans MS Bold.ttf", "")]),
    "serif": ("С засечками: история, тайна, драма", [(SUP + "Georgia Bold.ttf", ""), (SUP + "Times New Roman Bold.ttf", "")]),
    "typewriter": ("Печатная машинка: секреты, документы, криминал", [(SUP + "Courier New Bold.ttf", "")]),
    "rounded": ("Округлый дружелюбный", [(SUP + "Arial Rounded Bold.ttf", ""), (SUP + "Trebuchet MS Bold.ttf", "")]),
    "marker": ("Фломастер, от руки", [("/System/Library/Fonts/MarkerFelt.ttc", "Wide"), (SUP + "Chalkduster.ttf", "")]),
}
LOOKS = {
    "outline": "Белые буквы с чёрной обводкой, последняя строка жёлтая",
    "box": "Буквы на ярких плашках",
    "tilted": "Наклонная надпись",
    "shout": "Первое слово огромное и жёлтое",
    "red": "Красные буквы с белой обводкой",
}
_cache = {}


def _missing(font, ch: str) -> bool:
    """True when the font has no glyph for ch (it would draw the same box as for a non-existent character)."""
    try:
        return font.getmask(ch).tobytes() == font.getmask("\U0010FFFD").tobytes()
    except Exception:  # noqa: BLE001
        return True


def _load(key: str, size: int, text: str):
    """A FreeType font for this style that can write the text, or None."""
    for path, style in STYLE_FONTS.get(key, ("", []))[1]:
        if not Path(path).exists():
            continue
        for idx in range(0, 24 if path.endswith(".ttc") else 1):
            try:
                f = ImageFont.truetype(path, size, index=idx)
            except OSError:
                break
            if style and style.lower() not in " ".join(f.getname()).lower():
                continue
            probe = next((ch for ch in text if ch.isalpha()), "A")
            if _missing(f, probe):
                break
            return f
    return None


def _font(size: int, key: str = "", text: str = ""):
    if key:
        ck = (key, size, any("а" <= ch.lower() <= "я" or ch in "ёЁ" for ch in text))
        if ck not in _cache:
            _cache[ck] = _load(key, size, text)
        if _cache[ck]:
            return _cache[ck]
    for f in FONTS:
        if Path(f).exists():
            return ImageFont.truetype(f, size)
    return ImageFont.load_default(size)


def pick_style(text: str, font: str = "", look: str = "") -> tuple:
    """The writer's choice when valid; otherwise a different one from video to video (by the headline)."""
    h = sum(ord(ch) for ch in text or "x")
    if font not in STYLE_FONTS:
        font = list(STYLE_FONTS)[h % len(STYLE_FONTS)]
    if look not in LOOKS:
        look = list(LOOKS)[(h // 7) % len(LOOKS)]
    return font, look


def _wrap(draw, words, font, max_w):
    lines, cur = [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if cur and draw.textlength(test, font=font) > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = test
    if cur:
        lines.append(cur)
    return lines


def render(src: Path, out: Path, text: str, side: str = "left", vertical: bool = False, font: str = "", look: str = ""):
    """src: any image. out: JPEG 1280x720, or 1080x1920 for Shorts (under YouTube's 2 MB limit).
    side: left/right for a wide thumbnail; for a vertical one left = text at the top, right = at the bottom."""
    W, H = (1080, 1920) if vertical else (1280, 720)
    img = Image.open(src).convert("RGB")
    scale = max(W / img.width, H / img.height)
    img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    left, top = (img.width - W) // 2, (img.height - H) // 2
    img = img.crop((left, top, left + W, top + H))
    text = " ".join((text or "").upper().split())
    if text:
        # Darken the text side so the headline is readable on any picture.
        n = H if vertical else W
        shade = Image.new("L", (1, n) if vertical else (n, 1))
        for i in range(n):
            t = i / n if side == "right" else 1 - i / n
            v = int(200 * max(0.0, (t - 0.35) / 0.65) ** 1.2)
            shade.putpixel((0, i) if vertical else (i, 0), v)
        shade = shade.resize((W, H))
        img = Image.composite(Image.new("RGB", (W, H), (0, 0, 0)), img, shade)
        draw = ImageDraw.Draw(img)
        max_w, max_h = (int(W * 0.88), int(H * 0.3)) if vertical else (int(W * 0.56), int(H * 0.82))
        words = text.split()
        fkey, look = pick_style(text, font, look)
        for size in range(190 if vertical else 170, 40, -6):
            font = _font(size, fkey, text)
            lines = _wrap(draw, words, font, max_w)
            line_h = int(size * 1.08)
            if len(lines) <= (4 if vertical else 3) and line_h * len(lines) <= max_h and \
                    all(draw.textlength(l, font=font) <= max_w for l in lines):
                break
        block = line_h * len(lines) + (int(size * 0.2) * len(lines) if look == "box" else 0) \
            + (int(line_h * 0.18) if look == "shout" and len(lines) > 1 else 0)
        if vertical:
            y = int(H * 0.1) if side != "right" else int(H * 0.9) - block
        else:
            y = (H - block) // 2
        stroke = max(4, size // 14)
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        box_colors = [((255, 214, 0), (0, 0, 0)), ((230, 20, 30), (255, 255, 255)), ((255, 255, 255), (0, 0, 0))]
        for i, line in enumerate(lines):
            f = font
            if look == "shout" and i == 0 and len(lines) > 1:
                f = _font(int(size * 1.18), fkey, text)
                if draw.textlength(line, font=f) > max_w:
                    f = font
            tw = draw.textlength(line, font=f)
            if vertical:
                x = (W - tw) // 2
            else:
                x = 60 if side != "right" else W - 60 - tw
            last = i == len(lines) - 1 and len(lines) > 1
            if look == "box":
                bg, fg = box_colors[i % len(box_colors)]
                pad = int(size * 0.14)
                top, bottom = f.getbbox(line)[1], f.getbbox(line)[3]
                ld.rectangle([x - pad, y + top - pad, x + tw + pad, y + bottom + pad], fill=bg)
                ld.text((x, y), line, font=f, fill=fg)
            elif look == "red":
                ld.text((x, y), line, font=f, fill=(235, 20, 30), stroke_width=stroke + 2, stroke_fill=(255, 255, 255))
            elif look == "shout":
                ld.text((x, y), line, font=f, fill=(255, 214, 0) if i == 0 else (255, 255, 255),
                        stroke_width=stroke, stroke_fill=(0, 0, 0))
            else:
                ld.text((x, y), line, font=f, fill=(255, 214, 0) if last else (255, 255, 255),
                        stroke_width=stroke, stroke_fill=(0, 0, 0))
            y += int(line_h * (1.18 if f is not font else 1)) + (int(size * 0.2) if look == "box" else 0)
        if look == "tilted":
            layer = layer.rotate(5 if side != "right" else -5, resample=Image.BICUBIC, center=(W / 2, H / 2))
        img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
    tmp = out.with_suffix(".tmp.jpg")
    for q in (90, 82, 74, 66):  # YouTube accepts thumbnails up to 2 MB
        img.save(tmp, "JPEG", quality=q, optimize=True)
        if tmp.stat().st_size < 1_900_000:
            break
    tmp.replace(out)


def write_package(job_dir: Path, extras: dict, short: dict = None):
    """output/youtube.txt: title, description, tags (and the Short's), ready to paste."""
    from . import pipeline  # here: pipeline imports this module
    chapters = pipeline.chapters_text(job_dir.name) if (job_dir / "chapters.json").exists() else ""
    parts = [
        "=== ДЛИННОЕ ВИДЕО ===",
        "",
        "НАЗВАНИЕ:",
        extras.get("youtube_title", ""),
        f"(по-русски: {extras.get('youtube_title_ru', '')})",
        "",
        "ОПИСАНИЕ (тайм-коды уже внизу):" if chapters else "ОПИСАНИЕ:",
        f"{extras.get('description', '')}\n\n{chapters}".strip() if chapters else extras.get("description", ""),
        "",
        "ТЕГИ (вставить в поле «Теги»):",
        ", ".join(extras.get("tags", [])),
        "",
        f"НАДПИСЬ НА ПРЕВЬЮ: {extras.get('thumbnail_text', '')} ({extras.get('thumbnail_text_ru', '')})",
        "ПРЕВЬЮ: файл thumbnail.jpg в этой же папке",
    ]
    if short:
        parts += [
            "",
            "=== ШОРТС ===",
            "",
            "НАЗВАНИЕ:",
            f"{short.get('title', '')} #shorts",
            f"(по-русски: {short.get('title_ru', '')})",
            "",
            "ОПИСАНИЕ:",
            f"{short.get('title', '')}\n"
            + ("Полная история на нашем канале." if re.search("[а-яА-ЯёЁ]", short.get("title", "")) else
               "The full story is on our channel.") + "\n\n#shorts "
            + " ".join("#" + "".join(t.split()) for t in extras.get("tags", [])[:3]),
        ]
    out = job_dir / "output"
    out.mkdir(exist_ok=True)
    (out / "youtube.txt").write_text("\n".join(parts) + "\n", "utf-8")
