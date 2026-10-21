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


def _font(size: int):
    for f in FONTS:
        if Path(f).exists():
            return ImageFont.truetype(f, size)
    return ImageFont.load_default(size)


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


def render(src: Path, out: Path, text: str, side: str = "left", vertical: bool = False):
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
        for size in range(190 if vertical else 170, 40, -6):
            font = _font(size)
            lines = _wrap(draw, words, font, max_w)
            line_h = int(size * 1.08)
            if len(lines) <= (4 if vertical else 3) and line_h * len(lines) <= max_h and \
                    all(draw.textlength(l, font=font) <= max_w for l in lines):
                break
        block = line_h * len(lines)
        if vertical:
            y = int(H * 0.1) if side != "right" else int(H * 0.9) - block
        else:
            y = (H - block) // 2
        stroke = max(4, size // 14)
        for i, line in enumerate(lines):
            tw = draw.textlength(line, font=font)
            if vertical:
                x = (W - tw) // 2
            else:
                x = 60 if side != "right" else W - 60 - tw
            color = (255, 214, 0) if i == len(lines) - 1 and len(lines) > 1 else (255, 255, 255)
            draw.text((x, y), line, font=font, fill=color, stroke_width=stroke, stroke_fill=(0, 0, 0))
            y += line_h
    tmp = out.with_suffix(".tmp.jpg")
    for q in (90, 82, 74, 66):  # YouTube accepts thumbnails up to 2 MB
        img.save(tmp, "JPEG", quality=q, optimize=True)
        if tmp.stat().st_size < 1_900_000:
            break
    tmp.replace(out)


def write_package(job_dir: Path, extras: dict, short: dict = None):
    """output/youtube.txt: title, description, tags (and the Short's), ready to paste."""
    parts = [
        "=== ДЛИННОЕ ВИДЕО ===",
        "",
        "НАЗВАНИЕ:",
        extras.get("youtube_title", ""),
        f"(по-русски: {extras.get('youtube_title_ru', '')})",
        "",
        "ОПИСАНИЕ:",
        extras.get("description", ""),
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
