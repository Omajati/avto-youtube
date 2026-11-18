"""Paths and settings storage."""
import json
import os
import shutil
import threading
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("AY_DATA", APP_DIR / "data"))
JOBS_DIR = DATA_DIR / "jobs"
PROMPTS_DIR = DATA_DIR / "prompts"
PROMPTS_DEFAULT_DIR = APP_DIR / "prompts_default"
MUSIC_DIR = DATA_DIR / "music"
SETTINGS_FILE = DATA_DIR / "settings.json"
DB_FILE = DATA_DIR / "jobs.db"

for d in (DATA_DIR, JOBS_DIR, PROMPTS_DIR, MUSIC_DIR):
    d.mkdir(parents=True, exist_ok=True)

# The prompts folder only holds templates the owner edited; everything else comes from the program, so
# improvements in new versions reach the owner. Older versions copied every default there on first start:
# those untouched copies (all written in the same moment) are moved aside once, edited ones stay.
_EDITED_MARK = PROMPTS_DIR / ".edited"
if not _EDITED_MARK.exists():
    copies = sorted(PROMPTS_DIR.glob("*.txt"), key=lambda f: f.stat().st_mtime)
    if copies:
        first = copies[0].stat().st_mtime
        old = PROMPTS_DIR / "старые копии"
        for f in copies:
            if f.stat().st_mtime - first < 5:
                old.mkdir(exist_ok=True)
                shutil.move(str(f), str(old / f.name))
    _EDITED_MARK.write_text("Здесь лежат только шаблоны, которые вы меняли сами.\n", "utf-8")

SECRET_FIELDS = [
    "anthropic_key", "openai_key", "elevenlabs_key",
    "fal_key", "replicate_key", "together_key", "runware_key", "royal_key", "ig_token", "ig_user_token",
    "lumean_key", "fastgen_key", "octo_token", "github_token",
]

DEFAULTS = {
    # Script
    "llm_provider": "anthropic",
    "anthropic_model": "claude-opus-5-5",
    "opus_script_only": True,
    "step_effort": True,  # only the script is thought through at full depth; the other text steps think less
    "voice_provider": "elevenlabs",  # or "royal" (RoyalTechno) or "lumean" (Lumean): the same ElevenLabs voices
    "lumean_model": "eleven_multilingual_v2",
    "lumean_template_id": "",
    "subscribe_ask": True,
    "telegram_url": "",  # the owner's Telegram channel: the descriptions invite there
    "bait_pass": True,  # the first sentences, the title and the thumbnail phrase made as gripping as possible  # long voiced videos ask to subscribe and like at 30-40 seconds
    "motion_mode": "auto",  # maps and timelines drawn in place of some pictures (see motion.py)
    "atmos_mode": "auto",  # fog, rain, snow… drawn over the scenes where the script has them (see atmos.py)
    "atmos_volume": 0.35,  # loudness of the rain, wind and fire sounds under those scenes  # Opus/Fable write only the script and its editing; the other text steps use Sonnet
    "anthropic_effort": "high",
    "openai_model": "gpt-5",
    "niche": "real historical stories: dramatic, little-known true events from history",
    "duration_min": 10,
    "ideas_count": 5,  # topics the writer proposes at once; more on request ("Ещё темы")
    "quality_threshold": 8.0,
    "min_criterion_score": 7,
    "max_editor_rounds": 3,
    # Voice
    "elevenlabs_voice_id": "JBFqnCBsd6RMkjVDRZzb",
    "elevenlabs_voice_name": "George",
    "elevenlabs_model": "eleven_multilingual_v2",
    "voice_stability": 0.5,
    "voice_similarity": 0.75,
    "voice_style": 0.0,
    "voice_speed": 1.0,
    # Images
    "image_provider": "fal",
    "fal_model": "fal-ai/flux/dev",
    "replicate_model": "black-forest-labs/flux-dev",
    "openai_image_model": "gpt-image-2.5-flare",
    "openai_image_quality": "medium",
    "together_model": "black-forest-labs/FLUX.1-schnell",
    "runware_model": "runware:101@1",
    "royal_image_model": "nano-banana-2",
    "fastgen_model": "gemini_nano_banana_2_image_generate",
    "image_style": (
        "cinematic digital oil painting, dramatic chiaroscuro lighting, muted earthy "
        "color palette, rich texture, highly detailed, "
        "no text, no letters, no watermark"
    ),
    "image_parallel": 4,
    # Video
    "fps": 30,
    "words_per_scene": 22,
    "short_words_per_scene": 12,
    "motion_strength": 0.15,
    "music_volume": 0.12,
    "yt_cookies_browser": "",  # "" = try the installed browsers when YouTube asks to sign in, "none" = never
    "scene_gap": 0.3,
    "paragraph_gap": 0.6,
    "render_parallel": 3,
    "jobs_parallel": 3,  # videos made at the same time up to the assembly; the assembly itself goes one at a time
    "loudness_lufs": -14,
    # Subtitles burned into the picture (can be switched per video too)
    "subtitles_long": False,
    "subtitles_short": True,
    "subs_pos": "mid_low",
    "voice_tags": True,  # ElevenLabs v3: the AI places emotion tags ([whispers], [excited]) along the story  # height of the subtitles: low, mid_low, mid, center (see captions.POSITIONS)
    "cta_short": "both",  # animated Subscribe / Like buttons on Shorts (see cta.py)
    "cta_lang": "auto",  # their language: auto = the video's, ru, en
    # Animation of scenes (image-to-video on fal.ai)
    "anim_mode": "off",
    "anim_short": False,
    "video_model": "kling25",
    "anim_count": 3,
    # Effects that follow the script (sounds on cuts, punch-ins, shakes)
    "fx_mode": "light",
    # Instagram Reels
    "ig_token": "", "ig_user_token": "", "ig_user_id": "", "ig_username": "", "ig_page": "", "ig_token_time": 0.0,
    "ig_login": "",  # "instagram" when connected through «API setup with Instagram login» (no Facebook page)
    "ig_auto": False, "ig_ai_label": True,
    "char_reference": True,
    "images_manual": True,  # pictures are drawn by the owner elsewhere and uploaded; the program draws none
    "auto_check": True,  # the logic checker (scene descriptions) and the picture checker (each finished picture)
    "ref_model": "nano",
    "sfx_volume": 0.5,
    # One-click assembly of prepared project folders: EN, RU, DE, ES, horizontal 1080p.
    "folder_languages": "en",  # comma-separated: which language folders to build
    "folder_voice_en_id": "",
    "folder_voice_en_name": "",
    "folder_voice_ru_id": "",
    "folder_voice_ru_name": "",
    "folder_voice_de_id": "",
    "folder_voice_de_name": "",
    "folder_voice_es_id": "",
    "folder_voice_es_name": "",
    "folder_motion_inserts": True,
    "folder_music_source": "library",  # library, or "compose": a new track per project (Lumean or ElevenLabs)
    "music_provider": "auto",  # who composes music: auto (Lumean when it voices), lumean, elevenlabs
    # Publishing the built videos through YouTube Studio in Octo Browser profiles (one profile = one channel).
    "folder_thumbs": True,
    "folder_clean_frames": False,  # after a finished build the pictures the program drew are deleted (MP4 + thumbnail stay)  # draw each language's thumbnail together with its video
    "folder_upload": False,
    "folder_upload_privacy": "public",  # public, unlisted, private
    "folder_trash_mp4": True,  # a published and re-checked MP4 goes to the Mac's Trash
    "folder_thumb_review": True,  # stop with the thumbnails shown before anything goes to YouTube  # a few maps, timelines and charts in the first half of the video
    "folder_smart_mapping": False,
    "folder_scene_binding": "",  # hints, smart, even; empty = take it from folder_smart_mapping
    "folder_references": True,
    "folder_chapters": True,
    "folder_subtitles": True,
    "folder_subtitle_preset": "karaoke",
    "folder_subtitle_font": "auto",
    "folder_subtitle_size": "medium",
    "folder_subtitles_position": "mid_low",
    "folder_fast": False,  # «Быстрая сборка»: static frames, no effects, one encoding pass
    "folder_music": True,
    "folder_music_volume": 0.12,
    "folder_auto_graphics": True,
    "folder_edit_style": "auto",
    "folder_edit_tempo": "balanced",
    "folder_effect_level": "normal",
    "folder_render_speed": "fast",
    "folder_jobs_parallel": 2,
}

_lock = threading.Lock()


def load_settings() -> dict:
    with _lock:
        data = {}
        if SETTINGS_FILE.exists():
            try:
                data = json.loads(SETTINGS_FILE.read_text("utf-8"))
            except Exception:
                data = {}
        merged = dict(DEFAULTS)
        merged.update(data)
        for k in SECRET_FIELDS:
            merged.setdefault(k, "")
        return merged


def save_settings(new: dict) -> dict:
    current = load_settings()
    for k, v in new.items():
        if k in SECRET_FIELDS:
            # Empty or masked value means "keep the old key".
            if not v or "•" in str(v):
                continue
            v = str(v).strip()
        elif k in DEFAULTS and isinstance(DEFAULTS[k], bool):
            v = v is True or str(v).lower() in ("1", "true", "on", "yes")
        elif k in DEFAULTS and isinstance(DEFAULTS[k], (int, float)):
            try:
                v = type(DEFAULTS[k])(v)
            except (TypeError, ValueError):
                continue
        current[k] = v
    with _lock:
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(SETTINGS_FILE)
        try:
            os.chmod(SETTINGS_FILE, 0o600)
        except OSError:
            pass
    return current


def clear_settings(*keys):
    """Empties settings, secrets included (save_settings keeps a secret when it gets an empty value)."""
    current = load_settings()
    for k in keys:
        current[k] = DEFAULTS.get(k, "")
    with _lock:
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(SETTINGS_FILE)


def public_settings() -> dict:
    """Settings for the browser: keys are masked."""
    s = load_settings()
    for k in SECRET_FIELDS:
        v = s.get(k) or ""
        s[k] = ("••••" + v[-4:]) if v else ""
    return s
