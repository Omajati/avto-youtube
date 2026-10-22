"""Projects: a niche with its own ready settings (language, style, voice, format...) and its own videos."""
import json
import re
import time
import uuid

from .config import DATA_DIR
from .net import UserError

PROJ_DIR = DATA_DIR / "projects"

LANGUAGES = {"en": "Английский (США, весь мир)", "ru": "Русский (СНГ)"}

# Fields a project can set. Empty value = take it from the general settings.
FIELDS = {
    "name": "", "icon": "🎬", "preset": "", "language": "en", "niche": "", "image_style": "",
    "voice_id": "", "voice_name": "", "channel_id": "", "format": "long", "duration_min": 10, "make_short": True,
    "anim_mode": "off", "anim_short": False, "anim_count": 3, "anim_pause": False, "subs_video": False, "subs_short": True, "fx_mode": "light",
    "made_for_kids": False, "fiction": False,
    # Series: episodes remember what happened before
    "series": False, "series_bible": "", "season_plan": "",
    "en_voice_id": "", "en_voice_name": "", "en_channel_id": "",  # last choice for the English version
    "seo_ru": "",  # the owner's permanent tags and search phrases for every video of the project
    "short_examples": "",  # texts of Shorts the owner loves: a style reference for the writer
    "same_place": True,  # one reference picture per location so every scene there shows the same place
    "characters": [],  # ids from the shared character library used by default
    "items": [],  # ids from the shared library of items and products shown in the pictures by default
    "voice_mode": "mixed", "short_seconds": 50, "music_mode": "library",  # narrator / mixed (narrator + characters' lines) / dialogue (cartoon, mostly dialogue) / host (the hero tells it all)
    # Voice details (empty = general settings)
    "tts_model": "", "voice_stability": "", "voice_similarity": "", "voice_style": "", "voice_speed": "",
}
VOICE_NUM = ("voice_stability", "voice_similarity", "voice_style", "voice_speed")

_OIL = ("cinematic digital oil painting, dramatic chiaroscuro lighting, muted earthy color palette, rich texture, "
        "highly detailed, historically accurate clothing and architecture, no text, no letters, no watermark")

_ADULT_TOON = ("adult 2D animated cartoon, satirical graphic style with expressive exaggerated characters, bold "
               "ink outlines, flat colors with moody cinematic lighting, muted palette with sharp red accents, "
               "grim urban backgrounds, film noir atmosphere, no text, no letters, no watermark")

PRESETS = {
    "history": {
        "name": "История", "icon": "🏛️", "language": "en",
        "niche": "real historical stories: dramatic, little-known true events from history",
        "image_style": _OIL + ", 16th-19th century atmosphere",
    },
    "mystery": {
        "name": "Мистика и загадки", "icon": "🕯️", "language": "en",
        "niche": "unsolved mysteries and strange true events: disappearances, unexplained phenomena, eerie places",
        "image_style": ("dark atmospheric digital painting, moody fog, cold blue and amber lighting, cinematic "
                        "composition, mysterious, highly detailed, no text, no letters, no watermark"),
        "anim_mode": "off",
    },
    "truecrime": {
        "name": "Тру-крайм", "icon": "🔍", "language": "en",
        "niche": "true crime stories: famous and forgotten criminal cases, investigations, heists and cons, told without gore",
        "image_style": ("noir graphic illustration, high contrast shadows, desaturated colors with red accents, "
                        "cinematic framing, film grain, no gore, no text, no letters, no watermark"),
    },
    "sport": {
        "name": "Спорт", "icon": "⚽", "language": "en",
        "niche": "incredible true sports stories: comebacks, rivalries, scandals, legendary athletes and matches",
        "image_style": ("dynamic sports illustration, dramatic stadium lighting, motion, sweat and emotion, vivid "
                        "saturated colors, cinematic wide angle, no logos, no text, no letters, no watermark"),
        "subs_video": True, "anim_mode": "off",
    },
    "space": {
        "name": "Наука и космос", "icon": "🚀", "language": "en",
        "niche": "mind-blowing science and space stories: discoveries, missions, strange facts about the universe",
        "image_style": ("epic cinematic space and science illustration, volumetric light, deep blues and glowing "
                        "highlights, ultra detailed, awe-inspiring scale, no text, no letters, no watermark"),
    },
    "biography": {
        "name": "Биографии и мотивация", "icon": "🏆", "language": "en",
        "niche": "rise-and-fall and rags-to-riches life stories of real famous people, entrepreneurs and inventors",
        "image_style": ("cinematic portrait illustration, warm golden lighting, shallow depth of field, rich "
                        "colors, emotional, highly detailed, no text, no letters, no watermark"),
        "subs_video": True,
    },
    "myths": {
        "name": "Мифы и сказки (2D)", "icon": "🐉", "language": "en",
        "niche": "myths, legends and folk tales from around the world, retold as gripping stories",
        "image_style": ("2D animated illustration, hand-drawn cartoon style, clean bold outlines, flat cel shading, "
                        "soft muted color palette, expressive characters, detailed storybook background art, "
                        "no text, no letters, no watermark"),
        "anim_mode": "off",
    },
    "history_ru": {
        "name": "История (СНГ)", "icon": "📜", "language": "ru",
        "niche": "реальные исторические истории для русскоязычной аудитории: драматичные малоизвестные события, "
                 "включая историю России и СНГ",
        "image_style": _OIL + ", 16th-20th century atmosphere",
    },
    "mystery_ru": {
        "name": "Мистика (СНГ)", "icon": "👁️", "language": "ru",
        "niche": "загадочные реальные истории и необъяснимые события для русскоязычной аудитории",
        "image_style": ("dark atmospheric digital painting, moody fog, cold blue and amber lighting, cinematic "
                        "composition, mysterious, highly detailed, no text, no letters, no watermark"),
        "anim_mode": "off",
    },
    "psychology": {
        "name": "Психология", "icon": "🧠", "language": "en",
        "niche": "psychology explained through real stories and famous experiments: why people think, feel and act the "
                 "way they do, cognitive biases, manipulation, relationships, practical takeaways for everyday life",
        "image_style": ("cinematic conceptual illustration, soft dramatic lighting, symbolic surreal elements, muted "
                        "teal and warm orange palette, emotional human faces, highly detailed, no text, no letters, "
                        "no watermark"),
        "subs_video": True,
    },
    "mythbuster": {
        "name": "Разрушитель мифов", "icon": "💥", "language": "en",
        "niche": "myth busting: widely accepted 'facts', popular beliefs and textbook stories that turn out to be wrong, "
                 "exaggerated or more complicated; question the common version, show the evidence, separate proven "
                 "facts from speculation, never invent sources",
        "image_style": ("bold editorial illustration, high contrast, dramatic lighting, vivid accent colors, clean "
                        "composition, investigative mood, highly detailed, no text, no letters, no watermark"),
        "subs_video": True, "fx_mode": "strong",
    },
    "psychology_ru": {
        "name": "Психология (СНГ)", "icon": "🪞", "language": "ru",
        "niche": "психология простым языком через реальные истории и известные эксперименты: почему люди так думают и "
                 "поступают, когнитивные искажения, манипуляции, отношения, полезные выводы для жизни",
        "image_style": ("cinematic conceptual illustration, soft dramatic lighting, symbolic surreal elements, muted "
                        "teal and warm orange palette, emotional human faces, highly detailed, no text, no letters, "
                        "no watermark"),
        "subs_video": True,
    },
    "mythbuster_ru": {
        "name": "Разрушитель мифов (СНГ)", "icon": "🔨", "language": "ru",
        "niche": "разрушение мифов: общепринятые «факты», популярные убеждения и истории из учебников, которые на деле "
                 "оказываются неправдой или всё сложнее; ставим под сомнение привычную версию, показываем доказательства, "
                 "отделяем доказанное от догадок, не выдумываем источники",
        "image_style": ("bold editorial illustration, high contrast, dramatic lighting, vivid accent colors, clean "
                        "composition, investigative mood, highly detailed, no text, no letters, no watermark"),
        "subs_video": True, "fx_mode": "strong",
    },
    "kids": {
        "name": "Детские мультики", "icon": "🧸", "language": "en", "made_for_kids": True, "fiction": True, "voice_mode": "dialogue",
        "niche": "original animated stories for kids aged 3-8: kind adventures of cute animal friends and children "
                 "that gently teach friendship, honesty, courage, sharing and curiosity about the world",
        "image_style": ("bright 2D cartoon illustration for children, cute rounded characters with big expressive eyes, "
                        "soft cheerful pastel colors, clean bold outlines, friendly storybook backgrounds, warm gentle "
                        "lighting, no text, no letters, no watermark"),
        "subs_video": True, "subs_short": True, "anim_mode": "off", "fx_mode": "light", "duration_min": 5,
    },
    "kids_ru": {
        "name": "Детские мультики (СНГ)", "icon": "🎈", "language": "ru", "made_for_kids": True, "fiction": True, "voice_mode": "dialogue",
        "niche": "добрые мультики-сказки для детей 3-8 лет: приключения милых зверят и ребят, которые мягко учат "
                 "дружбе, честности, смелости, умению делиться и любопытству к миру",
        "image_style": ("bright 2D cartoon illustration for children, cute rounded characters with big expressive eyes, "
                        "soft cheerful pastel colors, clean bold outlines, friendly storybook backgrounds, warm gentle "
                        "lighting, no text, no letters, no watermark"),
        "subs_video": True, "subs_short": True, "anim_mode": "off", "fx_mode": "light", "duration_min": 5,
    },
    "adult_toons": {
        "name": "Взрослые мультики", "icon": "🎭", "language": "en", "fiction": True, "voice_mode": "dialogue",
        "niche": "animated stories for adults: dark satire and dramatic parables about the evil of the modern world: "
                 "deception, scams, greed, corruption, manipulation, hypocrisy, loneliness and the price of choices, "
                 "plus unusual human stories with a twist ending and a bitter or ironic moral",
        "image_style": _ADULT_TOON,
        "subs_video": True, "subs_short": True, "anim_mode": "off", "fx_mode": "light", "duration_min": 8,
    },
    "adult_toons_ru": {
        "name": "Взрослые мультики (СНГ)", "icon": "🃏", "language": "ru", "fiction": True, "voice_mode": "dialogue",
        "niche": "мультики для взрослых: острая сатира и драматичные притчи о зле современного мира: обман, "
                 "мошенники, жадность, коррупция, манипуляции, лицемерие, одиночество и цена выбора, а также "
                 "необычные человеческие истории с неожиданной концовкой и горькой или ироничной моралью",
        "image_style": _ADULT_TOON,
        "subs_video": True, "subs_short": True, "anim_mode": "off", "fx_mode": "light", "duration_min": 8,
    },
}

def _ps(key):
    return PRESETS[key]["image_style"]


# Picture styles to choose from (for projects and character portraits). desc tells the owner what it looks like.
IMAGE_STYLES = {
    "oil": {"name": "Кино, масляная живопись", "desc": "Как кадр из исторического фильма, написанный маслом: драматичный свет, тёплые землистые цвета.",
             "text": 'cinematic digital oil painting, dramatic chiaroscuro lighting, muted earthy color palette, rich texture, highly detailed, historically accurate clothing and architecture, 16th-19th century atmosphere, no text, no letters, no watermark'},
    "realistic": {"name": "Реалистичное фото", "desc": "Как настоящая фотография: живые лица, естественный свет.",
                   "text": "photorealistic cinematic photo, natural light, realistic skin texture, shallow depth of field, 35mm film look, highly detailed, no text, no letters, no watermark"},
    "dark": {"name": "Мрачная атмосфера", "desc": "Туман, холодный синий и янтарный свет, тревожное настроение. Для мистики.",
              "text": _ps("mystery")},
    "noir": {"name": "Нуар-графика", "desc": "Чёрно-белая контрастная графика с красными акцентами. Для тру-крайма.",
              "text": _ps("truecrime")},
    "portrait": {"name": "Тёплый кинопортрет", "desc": "Золотой свет, мягкий фон, вдохновляющее настроение. Для биографий.",
                  "text": _ps("biography")},
    "editorial": {"name": "Яркая журнальная иллюстрация", "desc": "Контрастная иллюстрация как в журнале, яркие акценты.",
                   "text": _ps("mythbuster")},
    "conceptual": {"name": "Сюрреалистичная концепт-иллюстрация", "desc": "Символы и образы, мягкий драматичный свет. Для психологии.",
                    "text": _ps("psychology")},
    "sport": {"name": "Спортивная динамика", "desc": "Свет стадиона, движение, эмоции.", "text": _ps("sport")},
    "space": {"name": "Космос и наука", "desc": "Эпичный космос, светящиеся синие тона.", "text": _ps("space")},
    "toon2d": {"name": "2D-мультфильм", "desc": "Рисованный мультфильм: чёткие контуры, плоские мягкие цвета.",
                "text": '2D animated illustration, hand-drawn cartoon style, clean bold outlines, flat cel shading, soft muted color palette, expressive characters, detailed storybook background art, consistent character design, historically accurate clothing and architecture, no text, no letters, no watermark'},
    "kids": {"name": "Детский мультик", "desc": "Милые круглые персонажи с большими глазами, яркие пастельные цвета.",
              "text": _ps("kids")},
    "adult_toon": {"name": "Взрослый мультик (нуар)", "desc": "Сатирическая 2D-анимация: выразительные персонажи, мрачный город, красные акценты.",
                    "text": _ADULT_TOON},
    "toon3d": {"name": "3D-мультфильм", "desc": "Как современный 3D-мультфильм студии: объёмные персонажи, мягкий свет.",
                "text": "3D animated film style, stylized characters with expressive faces, soft global illumination, rich colors, highly detailed render, no text, no letters, no watermark"},
    "anime": {"name": "Аниме", "desc": "Японская анимация: чистые линии, выразительные глаза, кинематографичный свет.",
               "text": "anime illustration, clean line art, cel shading, expressive eyes, cinematic lighting, detailed background, no text, no letters, no watermark"},
    "comic": {"name": "Комикс, графический роман", "desc": "Чернильные линии, густые тени, приглушённые цвета.",
               "text": 'graphic novel illustration, bold ink linework, dramatic shadows, limited muted color palette with strong accents, cinematic composition, historically accurate details, no speech bubbles, no text, no letters, no watermark'},
    "engraving": {"name": "Старинная гравюра", "desc": "Штриховка сепией на старой бумаге, как в книгах XIX века.",
                   "text": 'antique engraving illustration, fine cross-hatching, sepia ink on aged paper, 19th century book illustration style, highly detailed, historically accurate, no text, no letters, no watermark'},
}

# Voice settings that sound natural in Russian with ElevenLabs (the multilingual model handles Russian best).
RU_VOICE = {"tts_model": "eleven_multilingual_v2", "voice_stability": 0.45, "voice_similarity": 0.8,
            "voice_style": 0.15, "voice_speed": 1.0}


def _path(pid: str):
    if not re.fullmatch(r"[a-z0-9]{6,32}", pid or ""):
        raise UserError("Проект не найден")
    return PROJ_DIR / f"{pid}.json"


def _clean(data: dict) -> dict:
    out = {}
    for k, default in FIELDS.items():
        v = data.get(k, default)
        if isinstance(default, list):
            v = [str(x) for x in v if re.fullmatch(r"[a-z0-9]{6,32}", str(x))] if isinstance(v, list) else []
        elif isinstance(default, bool):
            v = v is True or str(v).lower() in ("1", "true", "on", "yes")
        elif isinstance(default, (int, float)):
            try:
                v = float(v)
            except (TypeError, ValueError):
                v = default
        else:
            v = str(v or "").strip()
        out[k] = v
    if out["language"] not in LANGUAGES:
        out["language"] = "en"
    for k in VOICE_NUM:
        try:
            out[k] = float(out[k]) if str(out[k]).strip() != "" else ""
        except ValueError:
            out[k] = ""
    if out["fx_mode"] not in ("off", "light", "strong"):
        out["fx_mode"] = "light"
    if out["format"] not in ("long", "short"):
        out["format"] = "long"
    if out["voice_mode"] not in ("narrator", "mixed", "dialogue", "host"):
        out["voice_mode"] = "mixed"
    out["short_seconds"] = int(min(max(out["short_seconds"] or 50, 15), 180))
    if out["music_mode"] not in ("library", "generate", "off"):
        out["music_mode"] = "library"
    if out["anim_mode"] not in ("off", "hook", "key", "all"):  # "custom" is chosen per video
        out["anim_mode"] = "off"
    try:
        out["anim_count"] = int(min(max(int(float(out["anim_count"] or 3)), 1), 60))
    except (TypeError, ValueError):
        out["anim_count"] = 3
    out["name"] = out["name"][:60] or "Без названия"
    out["icon"] = out["icon"][:4] or "🎬"
    return out


def list_all() -> list:
    PROJ_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for f in PROJ_DIR.glob("*.json"):
        try:
            items.append(json.loads(f.read_text("utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(items, key=lambda p: p.get("created", 0))


def get(pid: str) -> dict:
    if not pid:
        return {}
    try:
        return json.loads(_path(pid).read_text("utf-8"))
    except (OSError, ValueError, UserError):
        return {}


def save(pid: str, data: dict) -> dict:
    PROJ_DIR.mkdir(parents=True, exist_ok=True)
    old = get(pid) if pid else {}
    if pid and not old:
        raise UserError("Проект не найден")
    proj = {**_clean({**old, **data}), "id": pid or uuid.uuid4().hex[:12], "created": old.get("created", time.time())}
    p = _path(proj["id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(proj, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(p)
    if pid:
        _sync_template(proj)
    return proj


def _sync_template(proj: dict):
    """The owner's niche made from this project (or this project made from it) follows the project's edits."""
    items = list_templates()
    key = next((k for k, t in items.items() if t.get("from_project") == proj["id"]), None)
    if not key and proj.get("preset") in items:
        key = proj["preset"]
    if not key:
        return
    items[key] = {**{k: v for k, v in proj.items() if k not in _NOT_IN_TEMPLATE}, "from_project": proj["id"], "mine": True}
    TEMPLATES_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), "utf-8")


# The owner's own niches: a project saved as a template shows up next to the ready niches.
TEMPLATES_FILE = DATA_DIR / "niche_templates.json"
_NOT_IN_TEMPLATE = ("preset", "channel_id", "series_bible", "season_plan", "id", "created", "count")


def list_templates() -> dict:
    try:
        return json.loads(TEMPLATES_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_template(pid: str) -> str:
    proj = get(pid)
    if not proj:
        raise UserError("Проект не найден")
    items = list_templates()
    # Saving the same project again updates its niche instead of adding a copy.
    key = next((k for k, t in items.items() if t.get("from_project") == pid), None) \
        or (proj.get("preset") if proj.get("preset") in items else None) or "my_" + uuid.uuid4().hex[:8]
    items[key] = {**{k: v for k, v in proj.items() if k not in _NOT_IN_TEMPLATE}, "from_project": pid, "mine": True}
    TEMPLATES_FILE.parent.mkdir(parents=True, exist_ok=True)
    TEMPLATES_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), "utf-8")
    return key


def remove_template(key: str):
    items = list_templates()
    items.pop(key, None)
    TEMPLATES_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), "utf-8")


def all_presets() -> dict:
    return {**PRESETS, **list_templates()}


def create_from_preset(preset: str, settings: dict) -> dict:
    base = {k: v for k, v in all_presets().get(preset, {}).items() if k not in ("from_project", "mine")} or None
    if not base:
        raise UserError("Неизвестная ниша")
    data = {**{k: v for k, v in FIELDS.items()}, **(RU_VOICE if base.get("language") == "ru" else {}), **base,
            "preset": preset,
            "duration_min": base.get("duration_min", settings.get("duration_min", 10))}
    return save("", data)


def remove(pid: str):
    import shutil
    _path(pid).unlink(missing_ok=True)
    shutil.rmtree(PROJ_DIR / pid, ignore_errors=True)


# ---------------------------------------------------------------- series episodes

def _episodes_file(pid: str):
    _path(pid)  # checks the id
    return PROJ_DIR / pid / "episodes.json"


def list_episodes(pid: str) -> list:
    try:
        return sorted(json.loads(_episodes_file(pid).read_text("utf-8")), key=lambda e: e.get("num", 0))
    except (OSError, ValueError, UserError):
        return []


def _write_episodes(pid: str, items: list):
    f = _episodes_file(pid)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(f)


def save_episode(pid: str, ep: dict):
    """Adds or replaces the episode with this number."""
    items = [e for e in list_episodes(pid) if e.get("num") != ep.get("num")]
    _write_episodes(pid, items + [ep])


def remove_episode(pid: str, num: int):
    _write_episodes(pid, [e for e in list_episodes(pid) if e.get("num") != num])


def plan_line(proj: dict, num: int) -> str:
    """The owner's plan for episode `num` from the season plan (one line per episode), or ''."""
    lines = [re.sub(r"^\s*(серия|episode|ep\.?)?\s*\d+\s*[.):—-]*\s*", "", l, flags=re.I).strip()
             for l in (proj.get("season_plan") or "").splitlines() if l.strip()]
    return lines[num - 1] if 0 < num <= len(lines) else ""


# ---------------------------------------------------------------- characters
# One shared library of characters (data/characters). A project only remembers which of them it uses by default.

CHAR_DIR = DATA_DIR / "characters"
CHAR_FIELDS = {"name": "", "name_en": "", "role": "", "description_ru": "", "appearance": "", "personality": "",
               "extra_ru": "",
               "speech_ru": "",       # how the character talks: manner, pet words, sample lines (Russian)
               "voice_id": "", "voice_name": "",  # the character's own ElevenLabs voice for its lines        # the owner's additions for some scenes: other clothes, details (Russian)
               "style": "",           # picture style of the portrait (English, from IMAGE_STYLES or the owner's own)
               "style_project": ""}   # older characters: the project whose style the portrait was drawn in
_CID = re.compile(r"[a-z0-9]{6,32}")


def _migrate():
    """Characters used to live inside projects: move them into the shared library once."""
    import shutil
    if not PROJ_DIR.exists():
        return
    for d in PROJ_DIR.glob("*/characters"):
        pid = d.parent.name
        moved = []
        for f in d.glob("*.json"):
            CHAR_DIR.mkdir(parents=True, exist_ok=True)
            if not (CHAR_DIR / f.name).exists():
                shutil.move(str(f), CHAR_DIR / f.name)
                png = f.with_suffix(".png")
                if png.exists():
                    shutil.move(str(png), CHAR_DIR / png.name)
            moved.append(f.stem)
        shutil.rmtree(d, ignore_errors=True)
        proj = get(pid)
        if proj and moved:
            save(pid, {"characters": list(dict.fromkeys(proj.get("characters", []) + moved))})


def list_chars(ids=None) -> list:
    """All characters, or only those with these ids (in the library order)."""
    _migrate()
    if not CHAR_DIR.exists():
        return []
    out = []
    for f in CHAR_DIR.glob("*.json"):
        try:
            c = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if ids is not None and c.get("id") not in ids:
            continue
        c["has_portrait"] = (CHAR_DIR / f"{c['id']}.png").exists()
        c["refs"] = [f.name for f in list_refs(c["id"])]
        out.append(c)
    return sorted(out, key=lambda c: c.get("created", 0))


def get_char(cid: str) -> dict:
    if not _CID.fullmatch(cid or ""):
        return {}
    try:
        return json.loads((CHAR_DIR / f"{cid}.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_char(cid: str, data: dict) -> dict:
    old = get_char(cid) if cid else {}
    if cid and not old:
        raise UserError("Персонаж не найден")
    c = {k: str(data.get(k, old.get(k, v)) or "").strip() for k, v in CHAR_FIELDS.items()}
    if not c["name"]:
        raise UserError("Укажите имя персонажа")
    c.update(id=cid or uuid.uuid4().hex[:10], created=old.get("created", time.time()))
    CHAR_DIR.mkdir(parents=True, exist_ok=True)
    (CHAR_DIR / f"{c['id']}.json").write_text(json.dumps(c, ensure_ascii=False, indent=2), "utf-8")
    return c


def portrait_path(cid: str):
    if not _CID.fullmatch(cid or ""):
        raise UserError("Персонаж не найден")
    return CHAR_DIR / f"{cid}.png"


# Extra photos of a character from other sides (back, profile, details). The main portrait stays <cid>.png.
MAX_REFS = 8
_REF = re.compile(r"[a-z0-9]{6,32}\.png")


def refs_dir(cid: str):
    portrait_path(cid)  # checks the id
    return CHAR_DIR / f"{cid}_refs"


def list_refs(cid: str) -> list:
    d = refs_dir(cid)
    return sorted(d.glob("*.png"), key=lambda f: f.stat().st_mtime) if d.exists() else []


def ref_path(cid: str, name: str):
    if not _REF.fullmatch(name or ""):
        raise UserError("Фото не найдено")
    return refs_dir(cid) / name


def new_ref_path(cid: str):
    if len(list_refs(cid)) >= MAX_REFS:
        raise UserError(f"У персонажа уже {MAX_REFS} дополнительных фото. Удалите лишние, чтобы добавить новые.")
    d = refs_dir(cid)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{uuid.uuid4().hex[:10]}.png"


def make_main(cid: str, name: str):
    """Swaps an extra photo with the main portrait."""
    ref, main = ref_path(cid, name), portrait_path(cid)
    if not ref.exists():
        raise UserError("Фото не найдено")
    if main.exists():
        tmp = ref.with_suffix(".swap")
        main.replace(tmp)
        ref.replace(main)
        tmp.replace(ref)
    else:
        ref.replace(main)


def remove_char(cid: str):
    import shutil
    if not _CID.fullmatch(cid or ""):
        return
    (CHAR_DIR / f"{cid}.json").unlink(missing_ok=True)
    (CHAR_DIR / f"{cid}.png").unlink(missing_ok=True)
    shutil.rmtree(CHAR_DIR / f"{cid}_refs", ignore_errors=True)
    for proj in list_all():
        if cid in proj.get("characters", []):
            save(proj["id"], {"characters": [x for x in proj["characters"] if x != cid]})


# ---------------------------------------------------------------- items
# Products and objects that must look exactly like the owner's photos in every picture (data/items).
# Same layout as characters: <iid>.json, the main photo <iid>.png and more photos in <iid>_refs.

ITEM_DIR = DATA_DIR / "items"
ITEM_FIELDS = {"name": "", "name_en": "", "kind": "",  # товар, предмет, техника...
               "description_ru": "",  # the owner's words: what it is, how it is used
               "appearance": "",      # exact English visual description made from the photos
               "extra_ru": ""}        # the owner's notes for scenes: how it is held, where it stands (Russian)


def _item_id(iid: str):
    if not _CID.fullmatch(iid or ""):
        raise UserError("Предмет не найден")
    return iid


def list_items(ids=None) -> list:
    if not ITEM_DIR.exists():
        return []
    out = []
    for f in ITEM_DIR.glob("*.json"):
        try:
            it = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if ids is not None and it.get("id") not in ids:
            continue
        it["has_photo"] = item_photo_path(it["id"]).exists()
        it["refs"] = [f.name for f in list_item_refs(it["id"])]
        out.append(it)
    return sorted(out, key=lambda it: it.get("created", 0))


def get_item(iid: str) -> dict:
    if not _CID.fullmatch(iid or ""):
        return {}
    try:
        return json.loads((ITEM_DIR / f"{iid}.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_item(iid: str, data: dict) -> dict:
    old = get_item(iid) if iid else {}
    if iid and not old:
        raise UserError("Предмет не найден")
    it = {k: str(data.get(k, old.get(k, v)) or "").strip() for k, v in ITEM_FIELDS.items()}
    if not it["name"]:
        raise UserError("Укажите название предмета")
    it.update(id=iid or uuid.uuid4().hex[:10], created=old.get("created", time.time()))
    ITEM_DIR.mkdir(parents=True, exist_ok=True)
    (ITEM_DIR / f"{it['id']}.json").write_text(json.dumps(it, ensure_ascii=False, indent=2), "utf-8")
    return it


def item_photo_path(iid: str):
    return ITEM_DIR / f"{_item_id(iid)}.png"


def item_refs_dir(iid: str):
    return ITEM_DIR / f"{_item_id(iid)}_refs"


def list_item_refs(iid: str) -> list:
    d = item_refs_dir(iid)
    return sorted(d.glob("*.png"), key=lambda f: f.stat().st_mtime) if d.exists() else []


def item_ref_path(iid: str, name: str):
    if not _REF.fullmatch(name or ""):
        raise UserError("Фото не найдено")
    return item_refs_dir(iid) / name


def new_item_ref_path(iid: str):
    if len(list_item_refs(iid)) >= MAX_REFS:
        raise UserError(f"У предмета уже {MAX_REFS} дополнительных фото. Удалите лишние, чтобы добавить новые.")
    d = item_refs_dir(iid)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{uuid.uuid4().hex[:10]}.png"


def item_make_main(iid: str, name: str):
    ref, main = item_ref_path(iid, name), item_photo_path(iid)
    if not ref.exists():
        raise UserError("Фото не найдено")
    if main.exists():
        tmp = ref.with_suffix(".swap")
        main.replace(tmp)
        ref.replace(main)
        tmp.replace(ref)
    else:
        ref.replace(main)


def remove_item(iid: str):
    import shutil
    if not _CID.fullmatch(iid or ""):
        return
    (ITEM_DIR / f"{iid}.json").unlink(missing_ok=True)
    (ITEM_DIR / f"{iid}.png").unlink(missing_ok=True)
    shutil.rmtree(ITEM_DIR / f"{iid}_refs", ignore_errors=True)
    for proj in list_all():
        if iid in proj.get("items", []):
            save(proj["id"], {"items": [x for x in proj["items"] if x != iid]})


# ---------------------------------------------------------------- the project's library of successful scripts
# The owner marks videos that worked; the writer studies them before writing new ones for this project.

GOLDEN_MAX = 30


def _golden_file(pid: str):
    d = PROJ_DIR / _path(pid).stem
    d.mkdir(parents=True, exist_ok=True)
    return d / "golden.json"


def list_golden(pid: str) -> list:
    try:
        return json.loads(_golden_file(pid).read_text("utf-8"))
    except (OSError, ValueError):
        return []


def add_golden(pid: str, entry: dict) -> dict:
    items = [x for x in list_golden(pid) if not (x.get("job") == entry.get("job") and x.get("kind") == entry.get("kind"))]
    entry = {**entry, "id": uuid.uuid4().hex[:10], "added": time.time()}
    items = (items + [entry])[-GOLDEN_MAX:]
    _golden_file(pid).write_text(json.dumps(items, ensure_ascii=False, indent=1), "utf-8")
    return entry


def remove_golden(pid: str, gid: str):
    items = [x for x in list_golden(pid) if x.get("id") != gid]
    _golden_file(pid).write_text(json.dumps(items, ensure_ascii=False, indent=1), "utf-8")
