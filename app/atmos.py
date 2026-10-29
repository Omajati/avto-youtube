"""Weather and mood over the picture, chosen by the script: fog, rain, a storm, snow, dust, embers, old film.
Drawn by the program itself (textures made once and moved by FFmpeg), or the owner's own footage and sounds
from data/atmos/<kind>/ (an overlay video on a black background and/or an audio loop)."""
import random
import threading
from pathlib import Path

from .config import DATA_DIR
from . import media

ATMOS_DIR = DATA_DIR / "atmos"

KINDS = ["none", "fog", "rain", "storm", "snow", "dust", "embers", "grain"]
RU = {"none": "Без эффекта", "fog": "Туман", "rain": "Дождь", "storm": "Гроза (дождь и молнии)", "snow": "Снег",
      "dust": "Пыль в воздухе", "embers": "Искры и угли", "grain": "Старая плёнка"}
MODES = {"auto": "По сценарию: ИИ сам ставит туман, дождь, снег и т. п. там, где они есть в истории", "off": "Выключены"}
# On the montage page one kind can also be put over the whole video: the mode is then that kind.

PROMPT_BLOCK = """

ALSO choose the atmosphere of each fragment (field atmosphere): a weather or mood layer drawn over the picture.
- "fog": fog, mist, haze, a swamp, a mysterious or eerie moment.
- "rain": rain, a wet street, a sad or gloomy moment in the rain.
- "storm": a thunderstorm, lightning, a storm at sea, a dramatic night.
- "snow": snow, winter, a blizzard, the cold north.
- "dust": a desert, an old attic or ruins, sun rays in a dusty room, an explosion's aftermath.
- "embers": fire, a burning city, a forge, a battle at night, a campfire.
- "grain": the distant past told like an old film or archive footage, memories, a flashback.
- "none": everything else.
Use it only where the narration or the place clearly has it, and keep it on the following fragments while the story
stays in that place and weather (rain does not stop between two pictures of the same street). Most fragments are "none"."""

SOUND_RU = {"fog": "ветер", "rain": "шум дождя", "storm": "дождь и гром", "snow": "вьюга", "dust": "ветер",
            "embers": "треск огня"}

# Background sounds made with FFmpeg, so it works without downloading anything; the owner's own loops win.
_SYNTH = {
    "rain": "anoisesrc=d=12:c=pink:a=0.35,highpass=f=500,lowpass=f=7000,volume=0.9",
    "storm": "anoisesrc=d=12:c=pink:a=0.4,highpass=f=400,lowpass=f=6000[r];"
             "aevalsrc='0.8*(random(0)*2-1)*exp(-1.6*mod(t,6.3))*lt(mod(t,6.3),3)':d=12:s=44100,lowpass=f=160,volume=2.2[g];"
             "[r][g]amix=inputs=2:normalize=0",
    "fog": "anoisesrc=d=12:c=brown:a=0.5,lowpass=f=500,tremolo=f=0.15:d=0.6,volume=1.2",
    "snow": "anoisesrc=d=12:c=brown:a=0.5,highpass=f=150,lowpass=f=1500,tremolo=f=0.25:d=0.7,volume=1.1",
    "dust": "anoisesrc=d=12:c=brown:a=0.4,lowpass=f=700,tremolo=f=0.2:d=0.5",
    "embers": "aevalsrc='(random(0)*2-1)*gt(random(1),0.9993)*0.9+(random(2)*2-1)*0.03':d=12:s=44100,"
              "lowpass=f=5000,highpass=f=200,volume=1.3",
}

_VIDEO = (".mp4", ".mov", ".webm", ".m4v")
_AUDIO = (".mp3", ".wav", ".m4a", ".aac", ".ogg")


def own_dir(kind: str) -> Path:
    d = ATMOS_DIR / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def own_files(kind: str, ext: tuple) -> list:
    return sorted(f for f in own_dir(kind).iterdir() if f.suffix.lower() in ext and not f.name.startswith("."))


def kinds_of(scenes: list, mode: str) -> list:
    """The layer of each scene: the scene's own (AI's or the owner's choice), or one kind over the whole video."""
    if mode == "off":
        return ["none"] * len(scenes)
    if mode in KINDS and mode != "none":
        return [sc.get("atmosphere") if sc.get("atmosphere_own") and sc.get("atmosphere") in KINDS else mode
                for sc in scenes]
    return [sc.get("atmosphere") if sc.get("atmosphere") in KINDS else "none" for sc in scenes]


# ---------------------------------------------------------------- textures drawn once with PIL

_LOCK = threading.Lock()


def _texture(kind: str, w: int, h: int, layer: int) -> Path:
    """A transparent PNG that tiles: twice the frame height (falling things) or width (drifting fog).
    Scenes are assembled in parallel: one thread draws it, the others wait."""
    with _LOCK:
        return _draw(kind, w, h, layer)


def _draw(kind: str, w: int, h: int, layer: int) -> Path:
    cache = ATMOS_DIR / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / f"{kind}_{w}x{h}_{layer}.png"
    if out.exists():
        return out
    from PIL import Image, ImageDraw, ImageFilter
    rnd = random.Random(f"{kind}{layer}")
    if kind == "fog":
        tw, th = w * 2, h
        def layer_(div, blur):  # soft noise of one size, tiling left to right
            sw, sh = max(4, tw // div), max(3, th // div)
            small = Image.new("L", (sw, sh))
            small.putdata([rnd.randint(0, 255) for _ in range(sw * sh)])
            edge = max(1, sw // 8)
            small.paste(small.crop((0, 0, edge, sh)), (sw - edge, 0))
            return small.resize((tw, th), Image.BICUBIC).filter(ImageFilter.GaussianBlur(blur))
        from PIL import ImageChops
        alpha = ImageChops.add(layer_(48, h / 10).point(lambda v: v * 0.65), layer_(14, h / 30).point(lambda v: v * 0.35))
        alpha = alpha.point(lambda v: max(0, min(255, int((v - 95) * 3.2))))  # clouds with gaps between them
        # Thicker low in the frame, thinner at the top.
        grad = Image.linear_gradient("L").resize((tw, th))
        alpha = ImageChops.multiply(alpha, grad.point(lambda v: 90 + v * 0.65))
        img = Image.new("RGBA", (tw, th), (225, 230, 236, 0))
        img.putalpha(alpha)
        img.save(out.with_suffix(".tmp.png"))
        out.with_suffix(".tmp.png").replace(out)
        return out
    th = h
    img = Image.new("RGBA", (w, th), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    scale = h / 1080
    if kind in ("rain", "storm"):
        n, length, width_, alpha = (500, 38, 1, 70) if layer == 0 else (160, 80, 2, 105)
        for _ in range(int(n * w / 1920)):
            x, y = rnd.uniform(0, w), rnd.uniform(0, th)
            ln = length * scale * rnd.uniform(0.7, 1.3)
            for dy in (0, -th, th):  # drawn again across the edge, so the texture tiles
                d.line([(x, y + dy), (x - ln * 0.18, y + ln + dy)], fill=(210, 220, 235, alpha), width=max(1, int(width_ * scale)))
    elif kind == "snow":
        n, size = (380, 3) if layer == 0 else (110, 6)
        for _ in range(int(n * w / 1920)):
            x, y, r = rnd.uniform(0, w), rnd.uniform(0, th), size * scale * rnd.uniform(0.6, 1.4)
            for dy in (0, -th, th):
                d.ellipse([x - r, y - r + dy, x + r, y + r + dy], fill=(255, 255, 255, 200 if layer else 150))
        img = img.filter(ImageFilter.GaussianBlur(0.8 * scale if layer == 0 else 1.6 * scale))
    elif kind == "dust":
        for _ in range(int(260 * w / 1920)):
            x, y, r = rnd.uniform(0, w), rnd.uniform(0, th), 3 * scale * rnd.uniform(0.5, 1.6)
            for dy in (0, -th, th):
                d.ellipse([x - r, y - r + dy, x + r, y + r + dy], fill=(255, 238, 205, rnd.randint(90, 200)))
        img = img.filter(ImageFilter.GaussianBlur(1.2 * scale))
    elif kind == "embers":
        glow = Image.new("RGBA", (w, th), (0, 0, 0, 0))
        g = ImageDraw.Draw(glow)
        for _ in range(int((90 if layer == 0 else 40) * w / 1920)):
            x, y = rnd.uniform(0, w), rnd.uniform(0, th)
            r = (2.5 if layer == 0 else 4.5) * scale * rnd.uniform(0.6, 1.4)
            col = rnd.choice([(255, 170, 60), (255, 120, 30), (255, 210, 120)])
            for dy in (0, -th, th):
                g.ellipse([x - r * 3, y - r * 3 + dy, x + r * 3, y + r * 3 + dy], fill=col + (70,))
                d.ellipse([x - r, y - r + dy, x + r, y + r + dy], fill=col + (235,))
        img = Image.alpha_composite(glow.filter(ImageFilter.GaussianBlur(4 * scale)), img)
    full = Image.new("RGBA", (w, th * 2), (0, 0, 0, 0))
    full.paste(img, (0, 0))
    full.paste(img, (0, th))
    full.save(out.with_suffix(".tmp.png"))
    out.with_suffix(".tmp.png").replace(out)
    return out


# ---------------------------------------------------------------- the layer over one clip

def _graph(kind: str, w: int, h: int, seed: int) -> tuple:
    """(extra inputs, filter graph from [0:v] to [v]) for the program's own drawing of this kind."""
    s = seed % 7
    if kind == "grain":
        return [], "[0:v]eq=saturation=0.75:contrast=1.06,noise=alls=16:allf=t,vignette=PI/4.5,format=yuv420p[v]"
    if kind == "fog":
        t = _texture("fog", w, h, 0)
        return [t], (f"[0:v]eq=contrast=0.9:brightness=0.03:saturation=0.85[b];[1:v]format=rgba,colorchannelmixer=aa=0.85[f];"
                     f"[b][f]overlay=x='-mod(t*{w / 40:.1f}+{s * w / 7:.0f},w/2)':y=0:shortest=1,format=yuv420p[v]")
    falls = {"rain": (h * 2.2, h * 3.4, 1), "storm": (h * 2.4, h * 3.8, 1), "snow": (h * 0.07, h * 0.14, 1),
             "dust": (h * 0.025, h * 0.05, -1), "embers": (h * 0.12, h * 0.22, -1)}
    v0, v1, sign = falls[kind]
    t0, t1 = _texture(kind, w, h, 0), _texture(kind, w, h, 1)
    sway = {"snow": f"{w * 0.012:.0f}*sin(t*0.9+{s})", "embers": f"{w * 0.01:.0f}*sin(t*1.7+{s})",
            "dust": f"{w * 0.006:.0f}*sin(t*0.5+{s})"}.get(kind, "0")
    def y(v):
        return f"'-h/2+mod(t*{v:.1f}+{s * 97},h/2)'" if sign > 0 else f"'-mod(t*{v:.1f}+{s * 97},h/2)'"
    base = {"rain": "eq=brightness=-0.05:saturation=0.8:contrast=1.04,colorbalance=bs=0.06:bm=0.04",
            "storm": "eq=brightness=-0.09:saturation=0.75:contrast=1.08,colorbalance=bs=0.08:bm=0.05",
            "snow": "eq=brightness=0.02:saturation=0.85,colorbalance=bs=0.05:bh=0.03",
            "dust": "colorbalance=rh=0.04:gh=0.02",
            "embers": "colorbalance=rs=0.06:rm=0.05:bh=-0.04,eq=saturation=1.08"}[kind]
    if kind == "storm":  # flashes of lightning at the seed's own moments
        p = 5.3 + s * 0.7
        base += (f",eq=brightness='0.45*(lt(mod(t+{s * 1.3:.1f},{p:.1f}),0.06)+between(mod(t+{s * 1.3:.1f},{p:.1f}),0.14,0.2))'"
                 ":eval=frame")
    g = (f"[0:v]{base}[b];[1:v]format=rgba[l0];[2:v]format=rgba[l1];"
         f"[b][l0]overlay=x='{sway}':y={y(v0)}:shortest=1[m];[m][l1]overlay=x='{sway}':y={y(v1)}:shortest=1,format=yuv420p[v]")
    return [t0, t1], g


def apply(clip: Path, kind: str, w: int, h: int, fps: int, seed: int = 0):
    """Draws the atmosphere over a finished scene clip (replaces the file)."""
    if kind not in KINDS or kind == "none":
        return
    dur = media.video_duration(clip)
    tmp = clip.with_suffix(".atm.mp4")
    footage = own_files(kind, _VIDEO)
    if footage:
        src = footage[seed % len(footage)]
        g = (f"[1:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps={fps},format=gbrp[o];"
             f"[0:v]format=gbrp[m];[m][o]blend=all_mode=screen:shortest=1,format=yuv420p[v]")
        args = ["-i", clip, "-stream_loop", -1, "-i", src]
    else:
        extra, g = _graph(kind, w, h, seed)
        args = ["-i", clip]
        for t in extra:
            args += ["-loop", 1, "-framerate", fps, "-i", t]
    media.run([*args, "-filter_complex", g, "-map", "[v]", "-map", "0:a?", "-t", f"{dur:.3f}", "-r", fps,
               "-c:v", "libx264", "-preset", "veryfast", "-crf", 20, "-pix_fmt", "yuv420p", "-c:a", "copy", tmp],
              f"Эффект «{RU[kind]}»")
    tmp.replace(clip)


# ---------------------------------------------------------------- background sound under each run of scenes

def _sound(kind: str) -> Path:
    own = own_files(kind, _AUDIO)
    cache = ATMOS_DIR / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    if own:
        src = own[0]
        out = cache / f"snd_{kind}_{src.stem}_{int(src.stat().st_mtime)}.wav"
        if not out.exists():
            media.to_wav(src, out)
        return out
    out = cache / f"snd_{kind}_synth.wav"
    if not out.exists():
        media.run(["-filter_complex", _SYNTH[kind], "-ar", 44100, "-ac", 1, "-c:a", "pcm_s16le", "-t", 12, out],
                  "Звук эффекта")
    return out


def ambient(kinds: list, durations: list, sfx: Path, has_sfx: bool, volume: float) -> bool:
    """Mixes the background sound of each run of scenes into the sound-effects track (written to sfx).
    Returns whether the track has anything."""
    runs, t, i = [], 0.0, 0
    starts = []
    for d in durations:
        starts.append(t)
        t += d
    while i < len(kinds):
        k = kinds[i]
        j = i
        while j + 1 < len(kinds) and kinds[j + 1] == k:
            j += 1
        if k in _SYNTH:
            runs.append((k, starts[i], starts[j] + durations[j]))
        i = j + 1
    if not runs or volume <= 0:
        return has_sfx
    args, parts = [], []
    if has_sfx:
        args += ["-i", sfx]
        parts.append("[0:a]")
    for k, a, b in runs:
        n = len(parts)
        args += ["-stream_loop", -1, "-i", _sound(k)]
        d = b - a
        fade = min(1.2, d / 3)
        parts.append(f"[{n}:a]atrim=0:{d:.3f},asetpts=PTS-STARTPTS,afade=t=in:d={fade:.2f},"
                     f"afade=t=out:st={d - fade:.3f}:d={fade:.2f},volume={volume:.2f},adelay={int(a * 1000)}[s{n}];")
    chains = "".join(p for p in parts if p.endswith(";"))
    labels = "".join(p if not p.endswith(";") else f"[s{k}]" for k, p in enumerate(parts))
    out = sfx.with_suffix(".amb.wav")
    media.run([*args, "-filter_complex", f"{chains}{labels}amix=inputs={len(parts)}:normalize=0:duration=longest[a]",
               "-map", "[a]", "-ar", 44100, "-ac", 1, "-c:a", "pcm_s16le", "-t", f"{t + 0.5:.3f}", out], "Звук эффектов")
    out.replace(sfx)
    return True
