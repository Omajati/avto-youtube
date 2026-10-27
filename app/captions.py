"""Subtitles: word timings from ElevenLabs alignment (or estimated), written as .srt and as .ass for burning in."""
import json
import re
import shutil
from pathlib import Path

from .config import DATA_DIR
from . import media

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def font() -> tuple:
    """Copies a bold system font into data/fonts and returns (fonts_dir, family name)."""
    fdir = DATA_DIR / "fonts"
    fdir.mkdir(parents=True, exist_ok=True)
    for src in FONT_CANDIDATES:
        p = Path(src)
        if not p.exists():
            continue
        dst = fdir / p.name
        if not dst.exists():
            shutil.copy(p, dst)
        try:
            from PIL import ImageFont
            family = ImageFont.truetype(str(dst), 20).getname()[0]
        except Exception:
            family = p.stem.replace(" Bold", "")
        return fdir, family
    return fdir, "Arial"


def _scene_words(text: str, align_file: Path, speech: float) -> list:
    """[(word, start, end)] relative to the start of the scene audio."""
    words = []
    if align_file.exists():
        try:
            a = json.loads(align_file.read_text(encoding="utf-8"))
            chars, st, en = a["characters"], a["character_start_times_seconds"], a["character_end_times_seconds"]
            cur, cs, ce = "", None, None
            for ch, s, e in zip(chars, st, en):
                if ch.isspace():
                    if cur:
                        words.append((cur, cs, ce))
                    cur, cs = "", None
                    continue
                if cs is None:
                    cs = s
                cur += ch
                ce = e
            if cur:
                words.append((cur, cs, ce))
            if words:
                return words
        except (OSError, ValueError, KeyError):
            words = []
    # No alignment (demo or old voice files): spread words over the speech by length.
    tokens = text.split()
    total = sum(len(t) + 1 for t in tokens) or 1
    t = 0.0
    for tok in tokens:
        d = speech * (len(tok) + 1) / total
        words.append((tok, t, t + d))
        t += d
    return words


def timeline(dir_: Path, prefix: str, scenes: list, durations: list) -> list:
    """All words of the video with absolute times."""
    out, offset = [], 0.0
    for i, sc in enumerate(scenes):
        wav = dir_ / "audio" / f"{prefix}_{i:03d}.wav"
        speech = media.wav_duration(wav) if wav.exists() else durations[i]
        for w, s, e in _scene_words(sc["text"], dir_ / "audio" / f"{prefix}_{i:03d}.json", speech):
            out.append((w, offset + s, offset + e, i))
        offset += durations[i]
    return out


def group(words: list, max_words: int, max_chars: int) -> list:
    """Splits words into caption lines: [(text, start, end)]. Lines never cross a sentence end or a long pause,
    and a long sentence is cut into even parts, so no single word is left hanging on its own."""
    sentences, cur = [], []
    for w in words:
        if cur and w[1] - cur[-1][2] > 1.2:  # a long pause also ends a line
            sentences.append(cur)
            cur = []
        cur.append(w)
        if re.search(r"[.!?…]\W*$", w[0]):
            sentences.append(cur)
            cur = []
    if cur:
        sentences.append(cur)

    def fits(part):
        return len(part) <= max_words and (len(" ".join(x[0] for x in part)) <= max_chars or len(part) == 1)

    def text(part):
        return " ".join(x[0] for x in part)

    def split(sent):
        # Fewest lines first, then even lengths; a break after a comma is preferred.
        n = len(sent)
        best = [(0, 0.0, -1)] + [None] * n  # (lines, cost, previous break)
        for i in range(1, n + 1):
            for j in range(max(0, i - max_words), i):
                if best[j] is None or not fits(sent[j:i]):
                    continue
                t = text(sent[j:i])
                cost = best[j][1] + (max_chars - len(t)) ** 2
                if i < n and re.search(r"[,;:—]\W*$", t):
                    cost -= max_chars ** 2 / 4
                if i - j == 1 and n > 1:
                    cost += max_chars ** 2  # avoid a lonely word
                cand = (best[j][0] + 1, cost, j)
                if best[i] is None or cand[:2] < best[i][:2]:
                    best[i] = cand
        cuts, i = [], n
        while i > 0:
            j = best[i][2]
            cuts.append(sent[j:i])
            i = j
        return cuts[::-1]

    cues = []
    for sent in sentences:
        for part in split(sent):
            cues.append([text(part), part[0][1], part[-1][2]])
    # Keep each line on screen until the next starts (short pauses), but not over long silences.
    for a, b in zip(cues, cues[1:]):
        if b[1] - a[2] < 0.6:
            a[2] = b[1]
        else:
            a[2] += 0.3
    if cues:
        cues[-1][2] += 0.3
    return [tuple(c) for c in cues]


def _ts(t: float, srt: bool) -> str:
    t = max(t, 0)
    h, m, s = int(t // 3600), int(t % 3600 // 60), t % 60
    if srt:
        return f"{h:02d}:{m:02d}:{int(s):02d},{int(round((s - int(s)) * 1000)) % 1000:03d}"
    return f"{h}:{m:02d}:{s:05.2f}"


def write_srt(cues: list, out: Path):
    lines = []
    for n, (text, s, e) in enumerate(cues, 1):
        lines += [str(n), f"{_ts(s, True)} --> {_ts(e, True)}", text, ""]
    out.write_text("\n".join(lines), encoding="utf-8")


# Places a caption line can move to when its usual place is busy in the picture: (name, band of the frame height it
# covers, ASS alignment, margin from the edge as a share of the height). The first one is the usual place.
PLACES = {
    True: [("mid", (0.58, 0.71), 2, 0.30), ("upper", (0.19, 0.32), 8, 0.19)],
    False: [("bottom", (0.80, 0.95), 2, 0.065), ("top", (0.04, 0.17), 8, 0.045)],
}


def _busy(img: Path, band: tuple) -> float:
    """How much detail (edges) a band of the picture has: a calm band is easy to read text on."""
    from PIL import Image, ImageFilter, ImageStat
    try:
        with Image.open(img) as im:
            g = im.convert("L").resize((96, 170) if im.height > im.width else (170, 96))
    except OSError:
        return 0.0
    e = g.filter(ImageFilter.FIND_EDGES)
    h = e.height
    crop = e.crop((0, int(h * band[0]), e.width, max(int(h * band[0]) + 1, int(h * band[1]))))
    return ImageStat.Stat(crop).mean[0]


def scene_places(dir_: Path, prefix: str, scenes: list, durations: list, vertical: bool, blocked: dict = None) -> list:
    """For each scene, where its captions go: the usual place, unless that part of the picture is clearly busier
    (a face, text, many details) and another place is calm and not taken by an animation or button at that time."""
    blocked = blocked or {}
    places = PLACES[vertical]
    out, t = [], 0.0
    for i in range(len(scenes)):
        s, e = t, t + durations[i]
        t = e
        img = dir_ / "images" / f"{prefix}_{i:03d}.png"
        if not img.exists():
            out.append(places[0][0])
            continue
        base = _busy(img, places[0][1])
        best = places[0][0]
        for name, band, _, _ in places[1:]:
            if any(a < e and b > s for a, b in blocked.get(name, [])):
                continue
            if base > 14 and _busy(img, band) < base * 0.55:
                best = name
        out.append(best)
    return out


def write_ass(cues: list, out: Path, width: int, height: int, vertical: bool, family: str, margin_v: int = None,
              size: int = None, places: list = None):
    """size: the owner's font size (remake); the outline grows with it. places: the place name of each cue."""
    own = size
    if vertical:
        # Shorts: big centered words, a bit below the middle (away from the YouTube buttons).
        size, align, margin_v, outline, upper = 96, 2, int(height * 0.30) if margin_v is None else margin_v, 6, True
    else:
        size, align, margin_v, outline, upper = 60, 2, 70 if margin_v is None else margin_v, 4, False
    if own:
        outline = max(2, round(outline * own / size))
        size = own
    extra = "".join(f"Style: {name},{family},{size},&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,"
                    f"{outline},2,{al},80,80,{int(height * mv)},1\n" for name, _, al, mv in PLACES[vertical][1:]) if places else ""
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{family},{size},&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{outline},2,{align},80,80,{margin_v},1
{extra}
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    rows = []
    for n, (text, s, e) in enumerate(cues):
        t = text.upper() if upper else text
        t = t.replace("\\", "").replace("{", "(").replace("}", ")")
        style = places[n] if places and places[n] != PLACES[vertical][0][0] else "Default"
        rows.append(f"Dialogue: 0,{_ts(s, False)},{_ts(e, False)},{style},,0,0,0,,{t}")
    out.write_text(header + "\n".join(rows) + "\n", encoding="utf-8")


# The owner's choice of caption height: the distance of the lowest line from the bottom, as a share of the height.
# Shorts keep clear of the bottom fifth, where YouTube shows the title and the buttons.
POSITIONS = {"low": "Совсем внизу", "mid_low": "Ниже середины (обычно)", "mid": "Чуть ниже центра", "center": "По центру"}
POS_SHARE = {True: {"low": 0.17, "mid_low": 0.22, "mid": 0.30, "center": 0.42},
             False: {"low": 0.04, "mid_low": 0.065, "mid": 0.12, "center": 0.40}}


def build(dir_: Path, prefix: str, scenes: list, durations: list, width: int, height: int, smart: bool = False,
          blocked: dict = None, pos: str = "") -> dict:
    """Writes output/<name>.srt always and an .ass file next to the clips. Returns paths and font info.
    smart: a caption moves to a calmer part of the frame when its usual place is busy in that scene's picture."""
    vertical = height > width
    words = timeline(dir_, prefix, scenes, durations)
    cues = group(words, 3, 18) if vertical else group(words, 8, 42)
    places = per_scene = None
    if smart:
        try:
            per_scene = scene_places(dir_, prefix, scenes, durations, vertical, blocked)
        except Exception:  # noqa: BLE001 - a failed look at the pictures keeps the captions in their usual place
            per_scene = None
    if per_scene:
        starts = [sum(durations[:i]) for i in range(len(durations))]
        places = [per_scene[max([i for i, st in enumerate(starts) if st <= s + 0.01] or [0])] for _, s, _ in cues]
    name = "short_subtitles" if vertical else "subtitles"
    (dir_ / "output").mkdir(exist_ok=True)
    srt = dir_ / "output" / f"{name}.srt"
    write_srt(cues, srt)
    fdir, family = font()
    ass = dir_ / f"{name}.ass"
    share = POS_SHARE[vertical].get(pos or "mid_low", POS_SHARE[vertical]["mid_low"])
    write_ass(cues, ass, width, height, vertical, family, margin_v=int(height * share), places=places)
    moved = sum(1 for p in places or [] if p != PLACES[vertical][0][0])
    return {"srt": srt, "ass": ass, "fonts": fdir, "moved": moved}
