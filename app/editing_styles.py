"""Offline editing director for the local editor.

The director deliberately has a small public surface: a handful of coherent looks
and three intensity controls.  It turns those choices into per-scene decisions so
the editor stays approachable while finished videos do not all share one pattern.
"""
from __future__ import annotations

import hashlib
import random
import re
from collections import Counter


STYLES = {
    "cinematic": {
        "name": "Кинодокументалистика", "icon": "🎬", "color": "cinematic",
        "motions": ["pan_left", "pan_right"],
        "transitions": ["fade", "dissolve", "light_leak"],
        "subtitle": "documentary", "graphic": "cinematic", "voice": "warm",
        "description": "Плавная камера, сдержанный цвет и редкие выразительные акценты.",
    },
    "investigation": {
        "name": "Расследование", "icon": "🔎", "color": "cool",
        "motions": ["pan_left", "pan_right"],
        "transitions": ["fade", "flash", "film"],
        "subtitle": "box", "graphic": "broadcast", "voice": "radio",
        "description": "Документы, холодный цвет и напряжённые смысловые акценты.",
    },
    "archive": {
        "name": "Архивная хроника", "icon": "📽", "color": "archive",
        "motions": ["pan_left", "pan_right"],
        "transitions": ["film", "fade", "dissolve"],
        "subtitle": "clean", "graphic": "minimal", "voice": "warm",
        "description": "Тёплая плёнка, даты и спокойное движение старых фотографий.",
    },
    "dynamic": {
        "name": "Динамичный YouTube", "icon": "⚡", "color": "vivid",
        "motions": ["pan_left", "pan_right"],
        "transitions": ["flash", "light_leak", "dissolve"],
        "subtitle": "shorts", "graphic": "neon", "voice": "bright",
        "description": "Быстрые акценты, контрастная картинка и крупная типографика.",
    },
    "science": {
        "name": "Научпоп", "icon": "🧪", "color": "natural",
        "motions": ["pan_left", "pan_right"],
        "transitions": ["dissolve", "flash", "fade"],
        "subtitle": "documentary", "graphic": "glass", "voice": "natural",
        "description": "Чистая картинка, схемы, числа и понятный визуальный ритм.",
    },
    "mystery": {
        "name": "Мистика", "icon": "🌒", "color": "dark",
        "motions": ["pan_left", "pan_right"],
        "transitions": ["fade", "film", "flash"],
        "subtitle": "box", "graphic": "cinematic", "voice": "deep",
        "description": "Тёмный образ, медленные наезды и тревожные паузы.",
    },
    "minimal": {
        "name": "Премиальный минимализм", "icon": "◻️", "color": "natural",
        "motions": ["pan_left", "pan_right"],
        "transitions": ["dissolve", "fade"],
        "subtitle": "clean", "graphic": "minimal", "voice": "natural",
        "description": "Чистый монтаж, минимум эффектов и аккуратная типографика.",
    },
}

STYLE_IDS = set(STYLES)
TEMPOS = {"calm", "balanced", "dynamic"}
EFFECT_LEVELS = {"low", "normal", "juicy"}
COLOR_LOOKS = {"auto", "natural", "warm", "cool", "cinematic", "archive", "dark", "vivid", "bw"}
VOICE_TONES = {"auto", "natural", "warm", "deep", "bright", "radio"}
MOTIONS = {"none", "zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down", "diagonal_in", "punch_in"}
# The owner's decision (18.11.2026): only the side pans are used. Zooms, the diagonal and up/down pans shook
# (zoompan moves the picture by whole pixels: measured 0.3–0.6 px of speed jump per frame on zooms against
# ~0.06 px on side pans). Any other motion, also in older projects, is turned into a side pan when rendered.
SMOOTH = {"pan_left", "pan_right"}
SMOOTH_FOR = {"zoom_in": "pan_right", "diagonal_in": "pan_right", "pan_down": "pan_right",
              "zoom_out": "pan_left", "punch_in": "pan_left", "pan_up": "pan_left"}


def smooth_motion(motion: str) -> str:
    return motion if motion in SMOOTH or motion == "none" else SMOOTH_FOR.get(motion, "pan_right")
TRANSITIONS = {"cut", "fade", "dissolve", "flash", "light_leak", "film"}

COLOR_FILTERS = {
    "natural": "eq=contrast=1.025:saturation=1.035",
    "warm": "colorbalance=rs=.055:bs=-.035,eq=saturation=1.07:contrast=1.025",
    "cool": "colorbalance=bs=.055:rs=-.025,eq=saturation=.94:contrast=1.07",
    "cinematic": "eq=contrast=1.10:saturation=.90,colorbalance=bs=.025:rs=.015,vignette=PI/5",
    "archive": "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131,eq=contrast=1.05,vignette=PI/5,noise=alls=3:allf=t+u",
    "dark": "eq=brightness=-.045:contrast=1.14:saturation=.76,colorbalance=bs=.035,vignette=PI/4",
    "vivid": "eq=contrast=1.075:saturation=1.22",
    "bw": "hue=s=0,eq=contrast=1.10,vignette=PI/6",
}


def _seed(project: dict, revision: int) -> int:
    raw = f"{project.get('id')}:{project.get('title')}:{revision}".encode("utf-8")
    return int(hashlib.sha256(raw).hexdigest()[:16], 16)


def _text(project: dict) -> str:
    return (str(project.get("title") or "") + " " +
            str(project.get("transcript", {}).get("text") or "")).lower().replace("ё", "е")


def _scene_phrases(project: dict) -> list[str]:
    scenes = project.get("scenes", [])
    words = project.get("transcript", {}).get("words", [])
    if not scenes or not words:
        return [""] * len(scenes)
    ends, at = [], 0.0
    for scene in scenes:
        at += float(scene.get("duration") or 0)
        ends.append(at)
    phrases = [[] for _ in scenes]
    for raw in words:
        when = float(raw[1]) if len(raw) > 1 else 0
        index = next((i for i, end in enumerate(ends) if when < end), len(scenes) - 1)
        phrases[index].append(str(raw[0]))
    return [" ".join(items).lower().replace("ё", "е") for items in phrases]


def choose_style(project: dict, recent: list[str], revision: int) -> str:
    """Choose a fitting style while avoiding styles used by recent final exports."""
    text = _text(project)
    scores = Counter({key: 1 for key in STYLES})
    rules = {
        "investigation": r"тайн|секрет|расслед|документ|заговор|преступ|скрывал|катастроф|investigat|conspir|crime|hidden|cover.up|evidence",
        "archive": r"архив|истори|век|год|войн|импери|ссср|древн|хроник|archive|history|centur|war|empire|ancient|chronicle",
        "science": r"учен|исслед|наук|эксперимент|технолог|космос|открыт|теори|scient|research|experiment|geolog|fossil|asteroid|dinosaur|theory|paleont",
        "mystery": r"мист|загад|страш|прокля|исчез|аномал|взорв|смерт|озер|mystery|strange|curse|vanish|anomal|explode|death|deadly|lake",
        "dynamic": r"топ|самый|шок|невероят|срочно|за минут|ошибк|факт|top|shocking|unbeliev|urgent|mistake|facts?",
        "cinematic": r"путешеств|биограф|судьб|африк|океан|город|страна|природ|journey|biograph|destiny|africa|ocean|nature|wildlife|river|forest"
                     r"|семь|мать|сын|дочь|внук|муж|family|mother|daughter|grandson|husband"
                     r"|familia|madre|hijo|hija|nieto|marido|navidad|familie|mutter|sohn|tochter|enkel|weihnacht",
        "minimal": r"бизнес|бренд|дизайн|архитект|преми|интерьер|business|brand|design|architect|premium|interior",
    }
    for style, pattern in rules.items():
        hits = len(re.findall(pattern, text))
        if hits:
            scores[style] += min(8, 2 * hits)
    if re.search(r"\d", text):
        scores["science"] += 1
        scores["investigation"] += 1
    for rank, style in enumerate(recent[:4]):
        if style in scores:
            scores[style] -= 7 - rank
    best = max(scores.values())
    candidates = sorted(key for key, value in scores.items() if value == best)
    return random.Random(_seed(project, revision)).choice(candidates)


def build_plan(project: dict, recent: list[str]) -> dict:
    """Apply one coherent editing plan to the project and return its summary."""
    revision = int(project.get("plan_revision") or 0) + 1
    requested = str(project.get("edit_style") or "auto")
    style_id = requested if requested in STYLES else choose_style(project, recent, revision)
    style = STYLES[style_id]
    rng = random.Random(_seed(project, revision))
    tempo = str(project.get("edit_tempo") or "balanced")
    if tempo not in TEMPOS:
        tempo = "balanced"
    level = str(project.get("effect_level") or "normal")
    if level not in EFFECT_LEVELS:
        level = "normal"

    effect_share = {"low": .08, "normal": .18, "juicy": .29}[level]
    if tempo == "calm":
        effect_share *= .70
    elif tempo == "dynamic":
        effect_share *= 1.25
    if style_id == "minimal":
        effect_share *= .55
    effect_share = min(effect_share, .36)  # ordinary cuts must always dominate

    scenes = project.get("scenes", [])
    phrases = _scene_phrases(project)
    last_motion = ""
    moves_so_far: list[str] = []
    last_audio_accent = -99
    visible_transitions = 0
    max_visible_transitions = 0 if len(scenes) < 3 else max(1, int(len(scenes) * .34))
    transition_counts = Counter()
    motion_counts = Counter()
    sfx_counts = Counter()
    for index, scene in enumerate(scenes):
        phrase = phrases[index] if index < len(phrases) else ""
        if scene.get("kind") == "image":
            # Camera moves alternate with still shots (the owner, 18.11.2026): about every other picture stands
            # still, never two stills in a row, and a picture longer than 9 s always moves so it does not freeze.
            moving = [m for m in style["motions"] if m != "none"]
            last_move = next((m for m in reversed(moves_so_far) if m != "none"), "")
            pool = [m for m in moving if m != last_move] or moving
            motion = rng.choice(pool)
            still_chance = {"low": .6, "normal": .5, "juicy": .35}[level]
            if style_id == "minimal":
                still_chance += .10
            if (last_motion != "none" and float(scene.get("duration") or 0) <= 9.0
                    and rng.random() < still_chance):
                motion = "none"
            scene["motion"] = motion
            last_motion = motion
            moves_so_far.append(motion)
            motion_counts[motion] += 1
        if index == 0 and max_visible_transitions:
            transition = "fade" if style_id in {"cinematic", "archive", "mystery"} else "cut"
        elif visible_transitions < max_visible_transitions and rng.random() < effect_share:
            transition = rng.choice(style["transitions"])
        else:
            transition = "cut"
        if index and visible_transitions < max_visible_transitions and re.search(r"тем временем|глава|часть|спустя|позже|в другой|перенесемся|meanwhile|years later"
                                                                                   r"|mientras tanto|años después|meses después|semanas después|días después|más tarde|al día siguiente|capítulo"
                                                                                   r"|inzwischen|währenddessen|jahre später|monate später|wochen später|tage später|am nächsten tag|kapitel", phrase):
            transition = "fade"
        scene["transition"] = transition
        if transition != "cut":
            visible_transitions += 1
        transition_counts[transition] += 1
        scene["effect"], scene["sfx"] = "none", "none"
        accent_gap = {"low": 8, "normal": 5, "juicy": 3}[level]
        can_accent = index - last_audio_accent >= accent_gap
        if can_accent and re.search(r"взрыв|удар|рухнул|погиб|катастроф|шок|опас|убил|explod|crash|danger"
                                   r"|explot|golpe|choque|accidente|murió|peligr|disparo|grit[óo]"
                                   r"|unfall|starb|gefahr|schlag|schock|schrie", phrase):
            scene["effect"], scene["sfx"] = "punch_in", "impact"
        elif can_accent and re.search(r"тысяч|миллион|миллиард|огромн|весь мир|масштаб|thousand|million"
                                     r"|\bmiles\b|millones|enorme|todo el mundo|tausend|millionen|milliard|riesig|ganze welt", phrase):
            scene["effect"], scene["sfx"] = "zoom_out", "whoosh"
        elif can_accent and transition != "cut":
            scene["sfx"] = "swoosh" if transition in {"dissolve", "film"} else "whoosh"
        if scene["sfx"] != "none":
            last_audio_accent = index
            sfx_counts[scene["sfx"]] += 1
        graphic = scene.get("graphic")
        if isinstance(graphic, dict) and (graphic.get("auto") or not graphic.get("style")):
            graphic["style"] = style["graphic"]

    chosen_color = str(project.get("color_look") or "auto")
    if chosen_color not in COLOR_LOOKS or chosen_color == "auto":
        chosen_color = style["color"]
    project["resolved_color_look"] = chosen_color
    requested_voice = str(project.get("voice_tone") or "auto")
    project["resolved_voice_tone"] = requested_voice if requested_voice in VOICE_TONES - {"auto"} else style["voice"]
    project["subtitle_preset"] = style["subtitle"]
    project["graphics_enabled"] = bool(project.get("graphics_enabled") or project.get("graphics_auto"))
    project["plan_revision"] = revision
    project["plan"] = {
        "style": style_id,
        "style_name": style["name"],
        "description": style["description"],
        "color": chosen_color,
        "voice": project["resolved_voice_tone"],
        "tempo": tempo,
        "effects": level,
        "transitions": dict(transition_counts),
        "motions": dict(motion_counts),
        "sound_effects": dict(sfx_counts),
        "revision": revision,
    }
    return project["plan"]


def quality_report(project: dict) -> list[dict]:
    """Cheap, local checks that catch the most common automatic-editing problems."""
    scenes = project.get("scenes", [])
    issues: list[dict] = []
    if not scenes:
        return [{"level": "bad", "text": "Нет ни одной сцены."}]
    total = sum(float(s.get("duration") or 0) for s in scenes)
    long_stills = [i + 1 for i, s in enumerate(scenes)
                   if s.get("kind") == "image" and float(s.get("duration") or 0) > 9 and s.get("motion") == "none"]
    if long_stills:
        issues.append({"level": "warn", "text": f"Долгие неподвижные кадры: {', '.join(map(str, long_stills[:8]))}."})
    effect_share = sum(s.get("transition") != "cut" for s in scenes) / len(scenes)
    if effect_share > .38:
        issues.append({"level": "warn", "text": "Слишком много заметных переходов — ролик может выглядеть перегруженным."})
    sfx_share = sum(str(s.get("sfx") or "none") != "none" for s in scenes) / len(scenes)
    if sfx_share > .35:
        issues.append({"level": "warn", "text": "Звуковые акценты стоят слишком часто и могут утомлять."})
    motions = [s.get("motion") for s in scenes if s.get("kind") == "image"]
    if len(motions) >= 5 and len(set(motions)) <= 1:
        issues.append({"level": "warn", "text": "На всех фотографиях повторяется одно движение камеры."})
    names = [str(s.get("name") or "") for s in scenes]
    if len(names) != len(set(names)):
        issues.append({"level": "info", "text": "Некоторые исходники повторяются на таймлайне."})
    if project.get("voice") and project.get("music") and float(project.get("music_volume") or 0) > .28:
        issues.append({"level": "warn", "text": "Музыка может заглушать голос: её базовая громкость выше 28%."})
    if project.get("voice") and project.get("transcript", {}).get("status") != "done":
        issues.append({"level": "info", "text": "Whisper ещё не готов: смысловые акценты и субтитры ограничены."})
    if total > 30 and len(scenes) < max(4, int(total / 10)):
        issues.append({"level": "warn", "text": "Мало визуальных смен для такой длительности ролика."})
    if not issues:
        issues.append({"level": "good", "text": "Монтаж сбалансирован: явных проблем не найдено."})
    return issues
