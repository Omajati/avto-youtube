"""Background music: picks a fitting track from the music folder, or composes a new one with ElevenLabs Music.
Composed tracks are saved to the music folder with a Russian style label, so they can be reused for free."""
import json
import random
import re
import time
from pathlib import Path

from . import net
from .config import MUSIC_DIR
from .tts import BASE, _key

PRICE_PER_MIN = 0.30  # approximate USD, ElevenLabs Music API
LOOP_SECONDS = 150    # long videos get a 2.5-minute track that is looped under the voice
AUDIO = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")
STYLES_FILE = MUSIC_DIR / "styles.json"

MODES = {
    "library": "Из раздела «Музыка» (подходящий по стилю трек)",
    "generate": "Сочинять новую под каждый ролик (ElevenLabs, ~$0.30 за минуту)",
    "off": "Без музыки",
}


def styles() -> dict:
    """file name -> {"style_ru", "prompt", "made_for", "created"} for composed tracks."""
    try:
        return json.loads(STYLES_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _save_styles(data: dict):
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    STYLES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def set_style(name: str, style_ru: str):
    data = styles()
    data.setdefault(name, {})["style_ru"] = style_ru.strip()
    _save_styles(data)


def forget(name: str):
    data = styles()
    if data.pop(name, None) is not None:
        _save_styles(data)


def tracks() -> list:
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(f for f in MUSIC_DIR.iterdir() if f.suffix.lower() in AUDIO)


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-zа-яё]{4,}", (text or "").lower())}


def pick(mood: str = "") -> Path | None:
    """A track whose style label or file name shares the most words with the wanted mood; random among equals."""
    files = tracks()
    if not files:
        return None
    want = _words(mood)
    meta = styles()

    def score(f):
        m = meta.get(f.name, {})
        return len(want & _words(" ".join([f.stem, m.get("style_ru", ""), m.get("prompt", "")])))
    best = max(score(f) for f in files)
    return random.choice([f for f in files if score(f) == best])


def cost(seconds: float) -> float:
    return round(seconds / 60 * PRICE_PER_MIN, 2)


def length_for(total: float, vertical: bool) -> float:
    """Shorts get a track as long as the video; long videos a shorter track that loops."""
    return min(max(total + 2, 15), 120) if vertical else min(total + 2, LOOP_SECONDS)


def compose(settings, prompt: str, seconds: float, style_ru: str, made_for: str) -> Path:
    """Composes an instrumental track and saves it to the music folder. Returns its path."""
    body = {"prompt": prompt.strip()[:2000], "music_length_ms": int(max(10, min(seconds, 600)) * 1000),
            "model_id": "music_v1", "force_instrumental": True}
    r = net.request("ElevenLabs Music", "POST", f"{BASE}/music", params={"output_format": "mp3_44100_128"},
                    headers={"xi-api-key": _key(settings)}, json=body, timeout=600, tries=3)
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    label = re.sub(r"[^\w\s\-,]+", "", style_ru or "музыка").strip()[:50] or "музыка"
    out = MUSIC_DIR / f"{label} ({time.strftime('%d.%m %H-%M')}).mp3"
    n = 2
    while out.exists():
        out = MUSIC_DIR / f"{label} ({time.strftime('%d.%m %H-%M')}) {n}.mp3"
        n += 1
    tmp = out.with_suffix(".tmp")
    tmp.write_bytes(r.content)
    tmp.replace(out)
    data = styles()
    data[out.name] = {"style_ru": style_ru, "prompt": prompt, "made_for": made_for, "created": time.time()}
    _save_styles(data)
    return out
