"""ElevenLabs text-to-speech."""
import base64
import json
from pathlib import Path

from . import net
from .net import UserError

BASE = "https://api.elevenlabs.io/v1"

MODELS = [
    ("eleven_multilingual_v2", "Multilingual v2 — самый естественный, 1 кредит за символ"),
    ("eleven_turbo_v2_5", "Turbo v2.5 — в 2 раза дешевле, чуть проще звучит"),
    ("eleven_flash_v2_5", "Flash v2.5 — самый дешёвый и быстрый"),
    ("eleven_v3", "v3 — самый эмоциональный (может звучать нестабильно)"),
]


def _key(settings):
    k = settings.get("elevenlabs_key")
    if not k:
        raise UserError("Не указан ключ ElevenLabs. Вставьте его в Настройках, раздел «Озвучка».")
    return k


def list_voices(settings) -> list:
    r = net.request("ElevenLabs", "GET", f"{BASE}/voices", headers={"xi-api-key": _key(settings)}, timeout=60)
    out = []
    for v in r.json().get("voices", []):
        labels = {k.replace(" ", "_"): str(x).strip() for k, x in (v.get("labels") or {}).items() if x}
        auto = _audience(v)
        own = (settings.get("voice_audience") or {}).get(v["voice_id"])
        audience = own if own in ("ru", "en", "both") else auto
        flag, country = _country(labels.get("accent", ""), audience)
        out.append({"voice_id": v["voice_id"], "name": v.get("name", ""), "description": _desc_ru(labels),
                    "kind": _kind(labels, v.get("description") or ""), "flag": flag, "country": country,
                    "preview_url": v.get("preview_url", ""), "auto_audience": auto, "audience": audience})
    order = {"ru": 0, "en": 1, "both": 2}
    kinds = {"male": 0, "female": 1, "child": 2, "other": 3}
    return sorted(out, key=lambda x: (order.get(x["audience"], 3), kinds[x["kind"]], x["name"].lower()))


def library(settings, gender: str = "", use_case: str = "", search: str = "", page: int = 0) -> dict:
    """Russian voices from the public ElevenLabs Voice Library."""
    headers = {"xi-api-key": _key(settings)}
    params = {"page_size": 30, "language": "ru", "page": max(int(page or 0), 0)}
    if gender:
        params["gender"] = gender
    if use_case:
        params["use_cases"] = use_case
    if search:
        params["search"] = search.strip()[:80]
    d = net.request("ElevenLabs", "GET", f"{BASE}/shared-voices", headers=headers, params=params, timeout=60).json()
    try:
        mine = {v["voice_id"] for v in net.request("ElevenLabs", "GET", f"{BASE}/voices", headers=headers,
                                                   timeout=60).json().get("voices", [])}
    except UserError:
        mine = set()
    out = []
    for v in d.get("voices") or []:
        labels = {"gender": v.get("gender") or "", "age": v.get("age") or "",
                  "description": v.get("descriptive") or "", "use_case": v.get("use_case") or ""}
        out.append({"voice_id": v.get("voice_id", ""), "owner": v.get("public_owner_id", ""), "name": v.get("name", ""),
                    "kind": _kind(labels, ""), "description": _desc_ru(labels),
                    "about": (v.get("description") or "")[:220], "preview_url": v.get("preview_url", ""),
                    "users": int(v.get("cloned_by_count") or 0), "added": v.get("voice_id") in mine,
                    "free": v.get("free_users_allowed", True)})
    return {"voices": out, "has_more": bool(d.get("has_more"))}


def add_from_library(settings, owner: str, voice_id: str, name: str) -> str:
    """Adds a library voice to the account's own voices (the API can only speak with those). Returns its voice_id."""
    r = net.request("ElevenLabs", "POST", f"{BASE}/voices/add/{owner}/{voice_id}", headers={"xi-api-key": _key(settings)},
                    json={"new_name": (name or "Голос")[:100]}, timeout=60, tries=2)
    return r.json().get("voice_id") or voice_id


_WORDS_RU = {
    "male": "мужской", "female": "женский", "neutral": "нейтральный", "non-binary": "нейтральный",
    "young": "молодой", "middle aged": "средних лет", "middle_aged": "средних лет", "middle-aged": "средних лет",
    "old": "пожилой", "child": "детский", "teen": "подросток",
    "narration": "для рассказов", "narrative_story": "для рассказов", "narrative & story": "для рассказов",
    "conversational": "разговорный", "news": "дикторский, новости", "informative_educational": "обучающий",
    "educational": "обучающий", "characters": "для персонажей", "characters_animation": "для мультфильмов",
    "animation": "для мультфильмов", "social_media": "для соцсетей", "social media": "для соцсетей",
    "advertisement": "рекламный", "entertainment_tv": "для ТВ", "audiobook": "для аудиокниг", "meditation": "для медитаций",
    "video games": "для игр", "asmr": "ASMR",
    "calm": "спокойный", "deep": "низкий", "warm": "тёплый", "raspy": "хрипловатый", "soft": "мягкий",
    "strong": "сильный", "confident": "уверенный", "casual": "непринуждённый", "crisp": "чёткий",
    "authoritative": "властный", "friendly": "дружелюбный", "pleasant": "приятный", "gravelly": "грубоватый",
    "husky": "с хрипотцой", "intense": "напряжённый", "seductive": "чувственный", "upbeat": "бодрый",
    "energetic": "энергичный", "excited": "взволнованный", "sassy": "дерзкий", "mature": "зрелый",
    "professional": "профессиональный", "classy": "солидный", "expressive": "выразительный", "emotive": "эмоциональный",
    "wise": "мудрый", "smooth": "плавный", "resonant": "звучный", "playful": "игривый", "cute": "милый",
    "whispery": "шепчущий", "childish": "детский", "dramatic": "драматичный", "mysterious": "загадочный", "hoarse": "охрипший",
}


def _ru(word: str) -> str:
    w = word.lower().strip()
    return _WORDS_RU.get(w, word)


def _desc_ru(labels: dict) -> str:
    parts = [labels.get("age"), labels.get("description") or labels.get("descriptive"), labels.get("use_case")]
    return ", ".join(_ru(x) for x in parts if x)


def _kind(labels: dict, text: str) -> str:
    """male / female / child / other."""
    age = labels.get("age", "").lower()
    blob = " ".join([age, labels.get("description", ""), labels.get("descriptive", ""), text]).lower()
    if any(w in blob for w in ("child", "kid", "teen", "boy", "girl", "детск", "ребён", "ребен")):
        return "child"
    g = labels.get("gender", "").lower()
    return "male" if g == "male" else "female" if g == "female" else "other"


_COUNTRIES = [
    (("american", "us", "usa"), "🇺🇸", "США"), (("british", "english", "uk", "london", "essex"), "🇬🇧", "Великобритания"),
    (("australian",), "🇦🇺", "Австралия"), (("irish",), "🇮🇪", "Ирландия"), (("scottish",), "🏴", "Шотландия"),
    (("canadian",), "🇨🇦", "Канада"), (("indian",), "🇮🇳", "Индия"), (("african", "nigerian", "south african"), "🌍", "Африка"),
    (("russian",), "🇷🇺", "Россия и СНГ"), (("ukrainian",), "🇺🇦", "Украина"), (("belarusian",), "🇧🇾", "Беларусь"),
    (("kazakh",), "🇰🇿", "Казахстан"), (("german",), "🇩🇪", "Германия"), (("french",), "🇫🇷", "Франция"),
    (("spanish", "latin", "mexican"), "🇪🇸", "Испания / Латинская Америка"), (("italian",), "🇮🇹", "Италия"),
    (("swedish",), "🇸🇪", "Швеция"), (("transatlantic",), "🌐", "Англоязычные страны"),
]


def _country(accent: str, audience: str) -> tuple:
    a = accent.lower()
    for keys, flag, name in _COUNTRIES:
        if any(a == k or a.startswith(k + " ") or k in a.split() for k in keys):
            return flag, name
    if audience == "ru":
        return "🇷🇺", "Россия и СНГ"
    if audience == "en":
        return "🌐", "Англоязычные страны"
    return "🌐", "Не указано"


_EN_ACCENTS = ("american", "british", "australian", "irish", "english", "canadian", "scottish", "us", "uk")


def _audience(v: dict) -> str:
    """Who a voice suits: 'ru' (native Russian), 'en' (native English) or 'both' (not clear / multilingual)."""
    labels = {k: str(x).lower() for k, x in (v.get("labels") or {}).items()}
    if v.get("category") == "premade":
        return "en"  # the standard ElevenLabs voices are native English speakers
    verified = {str(x.get("language", "")).lower() for x in (v.get("verified_languages") or []) if isinstance(x, dict)}
    # A voice verified for many languages says nothing about its native one.
    langs = verified if len(verified) <= 2 else set()
    langs.add(labels.get("language", ""))
    langs.add(str((v.get("fine_tuning") or {}).get("language") or "").lower())
    accent = labels.get("accent", "")
    if "ru" in langs or "russian" in langs or "russian" in accent or any("а" <= ch <= "я" for ch in v.get("name", "").lower()):
        return "ru"
    if langs & {"en", "english"} or any(a in accent.split() or accent.startswith(a) for a in _EN_ACCENTS):
        return "en"
    return "both"


def account_info(settings) -> dict:
    r = net.request("ElevenLabs", "GET", f"{BASE}/user/subscription", headers={"xi-api-key": _key(settings)}, timeout=60)
    d = r.json()
    return {"tier": d.get("tier"), "used": d.get("character_count"), "limit": d.get("character_limit")}


def synthesize(settings, text: str, out_path: Path, previous_text: str = "", next_text: str = "") -> int:
    """Writes an mp3 file. Returns the number of characters billed."""
    model = settings.get("elevenlabs_model") or "eleven_multilingual_v2"
    voice = settings.get("elevenlabs_voice_id") or "JBFqnCBsd6RMkjVDRZzb"
    body = {
        "text": text,
        "model_id": model,
        "voice_settings": {
            "stability": float(settings.get("voice_stability", 0.5)),
            "similarity_boost": float(settings.get("voice_similarity", 0.75)),
            "style": float(settings.get("voice_style", 0.0)),
            "use_speaker_boost": True,
            "speed": float(settings.get("voice_speed", 1.0)),
        },
    }
    if model != "eleven_v3":
        # Context around the fragment keeps intonation continuous between scenes.
        if previous_text:
            body["previous_text"] = previous_text[-1000:]
        if next_text:
            body["next_text"] = next_text[:1000]
    headers = {"xi-api-key": _key(settings)}
    params = {"output_format": "mp3_44100_128"}
    audio, alignment = None, None
    try:
        # The timestamps endpoint also returns when each character is spoken: used for subtitles.
        r = net.request("ElevenLabs", "POST", f"{BASE}/text-to-speech/{voice}/with-timestamps",
                        params=params, headers=headers, json=body, timeout=300)
        d = r.json()
        audio = base64.b64decode(d.get("audio_base64") or "")
        alignment = d.get("alignment") or d.get("normalized_alignment")
    except (UserError, ValueError) as e:
        if "ключ" in str(e) or "денег" in str(e) or "лимит" in str(e) or "кредит" in str(e):
            raise
    if not audio or len(audio) < 500:
        r = net.request("ElevenLabs", "POST", f"{BASE}/text-to-speech/{voice}", params=params,
                        headers={**headers, "Accept": "audio/mpeg"}, json=body, timeout=300)
        audio = r.content
        alignment = None
        out_path.with_suffix(".json").unlink(missing_ok=True)
    if not audio or len(audio) < 500:
        raise UserError("ElevenLabs вернул пустой звук. Попробуйте ещё раз.")
    tmp = out_path.with_suffix(".part")
    tmp.write_bytes(audio)
    tmp.replace(out_path)
    if alignment and alignment.get("characters"):
        out_path.with_suffix(".json").write_text(json.dumps(alignment), encoding="utf-8")
    return len(text)
