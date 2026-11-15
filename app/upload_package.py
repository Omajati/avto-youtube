"""What goes to YouTube with a video built from a project folder: the owner's upload file
<LANG>/загрузка_YouTube_<LANG>.md (1. Название, 2. Превью, 3. Описание, 4. Теги, 5. Категория) and the thumbnail.

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
FONTS = ["/System/Library/Fonts/Supplemental/Arial Black.ttf", "/System/Library/Fonts/Supplemental/Impact.ttf",
         "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf"]


def find_file(lang_dir: Path) -> Path | None:
    files = [p for p in lang_dir.glob("*.md") if "загруз" in p.name.lower() or "upload" in p.name.lower()]
    return min(files, key=lambda p: len(p.name)) if files else None


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
    title = next((_plain(l).strip(" «»\"") for l in _section(secs, "назван", "title").splitlines() if l.strip()), "")
    description = _plain(_section(secs, "описан", "description"))
    description = re.sub(r"\n{3,}", "\n\n", description).strip()
    tags_raw = " ".join(_section(secs, "теги", "tags").split("\n")).strip()
    tags = [t.strip(" .#") for t in tags_raw.split(",") if t.strip(" .#")]
    category = _plain(_section(secs, "категор", "category").split("\n")[0]) if _section(secs, "категор", "category") else ""
    kids = bool(re.search(r"(?i)для детей|made for kids", category)) and not re.search(r"(?i)не для детей|not made for kids", category)
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
    return {"title": title[:100], "description": description[:5000], "tags": tags, "category": category, "kids": kids,
            "thumb_prompt": prompt, "thumb_lines": lines, "file": str(path)}


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


def finished_thumb(lang_dir: Path) -> Path | None:
    pics = [p for p in lang_dir.iterdir() if p.suffix.lower() in PIC_EXT
            and re.search(r"(?i)превью|thumb", p.stem) and "основа" not in p.stem.lower()] if lang_dir.is_dir() else []
    return min(pics, key=lambda p: len(p.name)) if pics else None


def make_thumb(root: Path, lang: str, lang_dir: Path, package: dict, settings: dict, english: dict | None) -> Path:
    """The thumbnail of one language (see the module text). Returns its path."""
    done = finished_thumb(lang_dir)
    if done:
        return done
    lines = package.get("thumb_lines") or []
    src_dir = next((p for p in root.iterdir() if p.is_dir() and p.name.lower() == "исходники"), root)
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
