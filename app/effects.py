"""Effects that follow the script: sound effects on cuts and key moments, camera punches and shakes."""
import array
import random
import wave
from pathlib import Path

from .config import DATA_DIR
from . import media

SFX_DIR = DATA_DIR / "sfx"

MODES = {
    "off": "Выключены",
    "light": "Мягко: звуки и наезды только в ключевых местах",
    "strong": "Ярко: звук на каждой смене кадра (для шортсов)",
}
EFFECTS = ["none", "punch_in", "zoom_out", "shake"]
SFX = ["none", "whoosh", "impact", "swoosh"]
EFFECT_RU = {"punch_in": "резкий наезд", "zoom_out": "отъезд", "shake": "тряска"}
SFX_RU = {"whoosh": "вжух", "impact": "удар", "swoosh": "перелистывание"}

PROMPT_BLOCK = """

ALSO choose effects for each fragment (fields effect and sfx). They make the video feel edited by a pro, but only when they match the words:
- effect "punch_in": a fast zoom-in for a shocking reveal, a key name or a line that should hit hard.
- effect "shake": a short camera shake for an explosion, a crash, a gunshot, a scream, a sudden shock.
- effect "zoom_out": a reveal of scale ("the whole city was gone", "thousands of them").
- effect "none": everything else. Most fragments must be "none".
- sfx "impact": a deep hit, together with punch_in or shake.
- sfx "whoosh": a transition sound for a jump in time or place, or the start of a new chapter of the story.
- sfx "swoosh": a light page-turn sound for a quick change of picture.
- sfx "none": everything else.
{limit}"""

LIMIT = {
    "long": "Use effect other than none on at most 1 in 6 fragments and sfx other than none on at most 1 in 5.",
    "short": "This is a Short: use effects on up to 1 in 3 fragments and an sfx on up to half of them.",
}

# Sounds made with FFmpeg, so the program works without downloading anything.
# Your own sounds can be put into data/sfx/<name>/ (mp3 or wav): one of them is picked at random.
_SYNTH = {
    "whoosh": "anoisesrc=d=0.7:c=pink:a=0.5,highpass=f=300,lowpass=f=6000,"
              "afade=t=in:d=0.35:curve=exp,afade=t=out:st=0.35:d=0.35,volume=1.6",
    "swoosh": "anoisesrc=d=0.32:c=white:a=0.35,highpass=f=1200,lowpass=f=9000,"
              "afade=t=in:d=0.12:curve=exp,afade=t=out:st=0.12:d=0.2,volume=0.9",
    "impact": "aevalsrc='0.9*sin(2*PI*(70-40*t)*t)*exp(-5*t)+0.35*(random(0)*2-1)*exp(-35*t)':d=0.9:s=44100,"
              "lowpass=f=3000,volume=1.4",
}
# How long before the cut the sound starts (a whoosh leads into the new picture).
_LEAD = {"whoosh": 0.3, "swoosh": 0.12, "impact": 0.0}


def _sample(name: str) -> Path:
    """A mono 44.1 kHz wav for this sound: the user's own file if there is one, otherwise a generated one."""
    SFX_DIR.mkdir(parents=True, exist_ok=True)
    own_dir = SFX_DIR / name
    own_dir.mkdir(exist_ok=True)
    own = [f for f in own_dir.iterdir() if f.suffix.lower() in (".mp3", ".wav", ".m4a", ".aac", ".ogg")]
    cache = SFX_DIR / ".cache"
    cache.mkdir(exist_ok=True)
    if own:
        src = random.choice(own)
        out = cache / f"{name}_{src.stem}_{int(src.stat().st_mtime)}.wav"
        if not out.exists():
            media.to_wav(src, out)
        return out
    out = cache / f"{name}_synth.wav"
    if not out.exists():
        media.run(["-f", "lavfi", "-i", _SYNTH[name], "-ar", 44100, "-ac", 1, "-c:a", "pcm_s16le", out],
                  "Звуковой эффект")
    return out


def _read(path: Path) -> array.array:
    with wave.open(str(path), "rb") as w:
        data = array.array("h")
        data.frombytes(w.readframes(w.getnframes()))
    return data


def plan(scenes: list, mode: str, short: bool) -> list:
    """[(scene index, effect, sfx)] actually used for this mode."""
    out = []
    for i, sc in enumerate(scenes):
        eff = sc.get("effect") if sc.get("effect") in EFFECTS else "none"
        sfx = sc.get("sfx") if sc.get("sfx") in SFX else "none"
        if mode == "off":
            eff, sfx = "none", "none"
        elif mode == "strong" and sfx == "none" and i > 0:
            sfx = "swoosh"  # every change of picture gets a light page-turn sound
        if eff in ("punch_in", "shake") and sfx in ("none", "swoosh") and mode != "off":
            sfx = "impact"
        out.append((i, eff, sfx))
    return out


def build_track(fx_plan: list, durations: list, out: Path, volume: float) -> bool:
    """Writes a mono wav with all sound effects at the right times. Returns False if there are none."""
    events, t = [], 0.0
    starts = []
    for d in durations:
        starts.append(t)
        t += d
    total = t
    for i, _eff, sfx in fx_plan:
        if sfx != "none" and i < len(starts):
            events.append((max(starts[i] - _LEAD[sfx], 0.0), sfx))
    if not events:
        return False
    rate = 44100
    track = array.array("h", bytes(2 * int((total + 1) * rate)))
    samples = {}
    for at, name in events:
        if name not in samples:
            samples[name] = _read(_sample(name))
        s = samples[name]
        pos = int(at * rate)
        end = min(len(track), pos + len(s))
        for k in range(pos, end):
            v = track[k] + int(s[k - pos] * volume)
            track[k] = 32767 if v > 32767 else (-32768 if v < -32768 else v)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(track.tobytes())
    return True
