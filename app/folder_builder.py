"""One-click assembly of externally prepared project folders.

The source folder remains the source of truth.  Existing S001... media are read in
place, missing pictures are generated into ``кадры``, and only the finished MP4 is
written to the language folder.  Audio and render intermediates stay in app data so
the owner's project folder does not fill up with service files.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from fastapi import APIRouter, Body

from . import folder_inbox
from . import captions, editing_styles, fast_render, net, graphics, images, llm, local_editor, media, motion, music, octo, tts, upload_package
from .config import DATA_DIR, load_settings, save_settings
from .net import UserError


router = APIRouter(prefix="/api/folder-builder", tags=["folder-builder"])
ROOT = DATA_DIR / "folder_builder"
QUEUE_FILE = ROOT / "queue.json"
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".avif", ".heic"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
SCENE_FILE = re.compile(r"^S(\d{3})", re.I)
SCENE_HEAD = re.compile(r"(?m)^###\s+(S\d{3})([^\n]*)$")
REF_HEAD = re.compile(r"(?m)^###\s+(R\d+)([^\n]*)$")
# The plain-text layout (сцены_генерация.txt): one paragraph per scene, «SCENE S001. prompt…», «REFERENCE R1. prompt…».
SCENE_LINE = re.compile(r"(?m)^\s*SCENE\s+(S\d{3})\b[.:]?[ \t]*")
REF_LINE = re.compile(r"(?m)^\s*REFERENCE\s+(R\d+)\b[.:]?[ \t]*")
_lock = threading.RLock()
_jobs: dict[str, dict] = {}
_scheduler: threading.Thread | None = None

LANGS = ("en", "ru", "de", "es")
LANG_NAMES = {"en": "English", "ru": "Russian", "de": "German", "es": "Spanish"}
LANG_RU = {"en": "английский", "ru": "русский", "de": "немецкий", "es": "испанский"}
# Words in the names of narration files besides the language code: «озвучка», voice, voz, Sprecher…
NARRATION_WORDS = ("озвуч", "voice", "narration", "voz", "locuc", "sprech", "vertonung", "текст")

FOLDER_OPTION_KEYS = (
    "folder_languages", "folder_motion_inserts", "folder_music_source", "folder_thumbs", "folder_clean_frames", "folder_upload", "folder_upload_privacy", "folder_thumb_review", "folder_trash_mp4",
    "folder_schedule", "folder_schedule_time", "folder_mp4_forever",
    "folder_voice_ru_id", "folder_voice_ru_name", "folder_voice_de_id", "folder_voice_de_name",
    "folder_voice_es_id", "folder_voice_es_name",
    "folder_voice_en_id", "folder_voice_en_name", "folder_smart_mapping", "folder_scene_binding", "folder_references", "folder_chapters", "folder_subtitles",
    "folder_subtitle_preset", "folder_subtitle_font", "folder_subtitle_size", "folder_subtitles_position",
    "folder_music", "folder_music_volume", "folder_auto_graphics", "folder_edit_style", "folder_edit_tempo",
    "folder_effect_level", "folder_render_speed", "folder_fast", "folder_jobs_parallel", "image_parallel", "folder_image_parallel", "render_parallel",
    "folder_inbox_dir", "folder_work_dir", "folder_inbox_autostart", "folder_github_repo", "folder_github_branch",
)
# What may differ from channel to channel (niche): the look and sound of the video and where it goes.
# Folders, threads and speed stay common for the whole Mac.
CHANNEL_OPTION_KEYS = (
    "folder_languages", "folder_fast", "folder_motion_inserts", "folder_music_source", "folder_thumbs", "folder_upload",
    "folder_upload_privacy", "folder_thumb_review", "folder_schedule", "folder_schedule_time",
    "folder_voice_ru_id", "folder_voice_ru_name", "folder_voice_de_id", "folder_voice_de_name",
    "folder_voice_es_id", "folder_voice_es_name", "folder_voice_en_id", "folder_voice_en_name",
    "folder_smart_mapping", "folder_scene_binding", "folder_references", "folder_chapters", "folder_subtitles",
    "folder_subtitle_preset", "folder_subtitle_font", "folder_subtitle_size", "folder_subtitles_position",
    "folder_music", "folder_music_volume", "folder_auto_graphics", "folder_edit_style", "folder_edit_tempo", "folder_effect_level",
)
INBOX_STATE_FILE = ROOT / "inbox_state.json"
INBOX_INTERVAL = 60
GITHUB_INTERVAL = 120


def channel_of(path) -> str:
    """The channel (niche) of a project is the name of the folder it lies in."""
    return folder_inbox._nfc(Path(str(path)).parent.name) if str(path or "") else ""


def channel_lang(channel: str) -> str:
    """«Животные EN» → en: the language at the end of the channel name, if any."""
    match = re.search(r"(?:^|[\s_-])(EN|RU|DE|ES)\s*$", channel or "", re.I)
    return match.group(1).lower() if match else ""


def channel_custom(settings: dict, channel: str) -> dict:
    stored = (settings.get("folder_channel_settings") or {}).get(folder_inbox._nfc(channel or "")) or {}
    return {k: v for k, v in stored.items() if k in CHANNEL_OPTION_KEYS}


def channel_family(channel: str) -> str:
    """«Животные - Звери EN» → «Животные EN»; a channel without « - » has no family."""
    name = folder_inbox._nfc(channel or "").strip()
    if " - " not in name:
        return ""
    head, lang = name.split(" - ", 1)[0].strip(), channel_lang(name)
    parent = f"{head} {lang.upper()}" if lang else head
    return parent if parent != name else ""


def channel_parent(settings: dict, channel: str) -> str:
    """«Животные - Звери EN» belongs to the family «Животные EN»: while the family channel has its own settings,
    every «Животные - … EN» channel builds with them (its own settings, if any, go on top). Octo profiles stay per channel."""
    parent = channel_family(channel)
    return parent if parent and channel_custom(settings, parent) else ""


def channel_work_dir(settings: dict, channel: str) -> tuple[str, str]:
    """Where the channel's projects are unpacked and built: (folder, channel whose folder it is).
    The channel's own folder, else its family's («Животные EN» for «Животные - Звери EN»), else the common one ("")."""
    dirs = settings.get("folder_channel_work_dirs") or {}
    name = folder_inbox._nfc(channel or "").strip()
    for owner in (name, channel_family(name)):
        raw = str(dirs.get(owner) or "").strip() if owner else ""
        if raw:
            return raw, owner
    return str(settings.get("folder_work_dir") or "").strip(), ""


def channel_settings(settings: dict, channel: str) -> dict:
    """Common settings, then the family channel's (see channel_parent), then the channel's own on top.
    Without languages of its own a channel named «… EN» builds English."""
    out = dict(settings)
    if not channel:
        return out
    parent = channel_parent(settings, channel)
    custom = {**(channel_custom(settings, parent) if parent else {}), **channel_custom(settings, channel)}
    if "folder_languages" not in custom and channel_lang(channel):
        out["folder_languages"] = channel_lang(channel)
    out.update(custom)
    return out


def known_channels(settings: dict) -> list[dict]:
    names = set(settings.get("folder_channel_settings") or {}) | set(settings.get("folder_channels_extra") or [])
    names |= set(settings.get("folder_channel_work_dirs") or {})
    names |= {key.split("|", 1)[0] for key in (settings.get("octo_rules") or {}) if "|" in key}
    with _lock:
        names |= {channel_of(job.get("path")) for job in _jobs.values() if job.get("path")}
    for key in ("folder_inbox_dir", "folder_work_dir"):
        raw = str(settings.get(key) or "").strip()
        try:
            base = Path(raw).expanduser() if raw else None
            if base and base.is_dir():
                names |= {child.name for child in base.iterdir() if child.is_dir() and not child.name.startswith(("_", "."))
                          and not folder_inbox.is_temp_name(child.name)}
        except OSError:
            pass
    names = {folder_inbox._nfc(n).strip() for n in names if n and n.strip()}
    return [{"name": n, "custom": bool(channel_custom(settings, n)), "parent": channel_parent(settings, n),
             "langs": selected_langs(channel_settings(settings, n))} for n in sorted(names, key=str.lower)]


def _save_options(options: dict, channel: str = "", changed: bool = True):
    """Saves the page controls. With a channel chosen its look and sound go to that channel only, and only once the
    owner changed something there (just looking at a channel must not give it its own settings)."""
    options = {k: v for k, v in (options or {}).items() if k in FOLDER_OPTION_KEYS}
    channel = folder_inbox._nfc(str(channel or "")).strip()
    if not channel:
        save_settings(options)
        return
    own = {k: v for k, v in options.items() if k in CHANNEL_OPTION_KEYS}
    common = {k: v for k, v in options.items() if k not in CHANNEL_OPTION_KEYS}
    stored = dict(load_settings().get("folder_channel_settings") or {})
    if own and (changed or stored.get(channel)):
        stored[channel] = {**(stored.get(channel) or {}), **own}
    save_settings({**common, "folder_channel_settings": stored})


class _PauseRequested(Exception):
    pass


class _StopRequested(Exception):
    pass


class _ReviewRequested(Exception):
    """The thumbnails are ready and wait for the owner before anything goes to YouTube."""


_profile_locks: dict[str, threading.Lock] = {}


def _profile_lock(uuid: str) -> threading.Lock:
    with _lock:
        return _profile_locks.setdefault(uuid, threading.Lock())


def _now() -> int:
    return int(time.time())


def _job_id(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:12]


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)


def _load_queue():
    global _jobs
    ROOT.mkdir(parents=True, exist_ok=True)
    data = _read_json(QUEUE_FILE, {})
    _jobs = data if isinstance(data, dict) else {}
    changed = False
    for job in _jobs.values():
        status = job.get("status")
        if status in ("running", "queued"):
            job.update(status="error", step="", error="Сборка была прервана. Нажмите «Запустить» — готовые этапы повторяться не будут.")
            changed = True
        elif status == "pausing":
            job.update(status="paused", step="На паузе", error="")
            changed = True
        elif status == "stopping":
            job.update(status="stopped", step="Остановлено", error="")
        # "review" stays as it is: the thumbnails wait for the owner across restarts.
            changed = True
    if changed:
        _save_queue()
    threading.Thread(target=_clean_leftovers, daemon=True, name="folder-clean").start()


def _save_queue():
    with _lock:
        public = {key: {k: v for k, v in job.items() if k != "_thread"} for key, job in _jobs.items()}
        try:
            _write_json(QUEUE_FILE, public)
        except OSError:
            # A full disk must not kill the builds: the queue lives in memory and is written at the next change.
            QUEUE_FILE.with_suffix(QUEUE_FILE.suffix + ".tmp").unlink(missing_ok=True)


GB = 1024 ** 3


def _free_bytes() -> int:
    return shutil.disk_usage(DATA_DIR).free


def _montage_need(seconds: float) -> int:
    """Disk space one montage takes at its peak: the scene clips, the joined video, the mixed one and the one with
    subtitles burned in are each about the size of the final file (~0.06 GB a minute in 1080p), plus a margin."""
    return int((4 * seconds / 60 * .06 + 3) * GB)


def _folder_montage_dirs() -> set[Path]:
    with _lock:
        paths = [str(job.get("path") or "") for job in _jobs.values()]
    return {local_editor.ROOT / _project_id(path, lang) for path in paths if path for lang in LANGS}


def _clean_old_montages(keep: set[Path]) -> int:
    """Removes leftovers of earlier montages of queued folders (not the owner's own montages in «Локальный монтаж»):
    they are only a cache, rebuilt from the pictures and the voice-over at no cost."""
    freed = 0
    with local_editor._lock:
        busy = {local_editor.ROOT / pid for pid in local_editor._rendering}
    for folder in _folder_montage_dirs() - keep - busy:
        if not folder.is_dir():
            continue
        for base, _dirs, files in os.walk(folder):
            for name in files:
                try:
                    freed += (Path(base) / name).stat().st_size
                except OSError:
                    pass
        shutil.rmtree(folder, ignore_errors=True)
    return freed


def _wait_for_space(job_id: str, need: int, keep: set[Path] = frozenset(), what: str = "монтажа"):
    """Waits (pause and stop still work) until the disk has room; first clears the program's own old montages."""
    if _free_bytes() >= need:
        return
    _clean_old_montages(set(keep))
    while _free_bytes() < need:
        free = _free_bytes()
        _update(job_id, step=f"Мало места на диске: для {what} нужно ~{need / GB:.0f} ГБ, свободно {free / GB:.1f} ГБ. "
                             f"Освободите {max(1, (need - free) / GB):.0f} ГБ (корзина, старые ролики) — продолжу сам.")
        _sleep_checked(job_id, 60)


def _update(job_id: str, **changes):
    with _lock:
        if job_id not in _jobs:
            return
        _jobs[job_id].update(changes, updated=_now())
        _save_queue()


def _check_control(job_id: str):
    """Stop only between durable operations, so completed API results stay cached."""
    with _lock:
        status = (_jobs.get(job_id) or {}).get("status")
    if status == "pausing":
        raise _PauseRequested()
    if status == "stopping":
        raise _StopRequested()


def _extract_code(block: str, marker: str) -> str:
    chunks = re.findall(r"```[^\n]*\n(.*?)```", block, re.S)
    return next((x.strip() for x in chunks if marker.lower() in x.lower()), chunks[0].strip() if chunks else "")


def _chapter(raw: str) -> str:
    raw = re.sub(r"^[^A-Za-zА-Яа-я0-9]+", "", raw).strip()
    parts = [x.strip() for x in raw.split("·")]
    if len(parts) >= 3:
        return parts[1]
    if len(parts) == 2 and not re.search(r"\d:\d", parts[1]):
        return parts[1]
    return ""


def _paragraph(text: str, start: int) -> str:
    """The text from start to the first empty line: one scene or reference of the plain layout."""
    return re.split(r"\n\s*\n", text[start:], maxsplit=1)[0].strip()


def _plain_ref_keys(prompt: str) -> list[str]:
    """Regexes that tie a plain-layout reference to the scenes showing it: the ages of its people
    («71-year-old», «the old widow is 71»), its subject («young couple», «living room», «toy locomotive»)."""
    low = prompt.lower()
    keys = []
    for age in dict.fromkeys(re.findall(r"\b(\d{1,2})-year-old\b", low)):
        keys.append(rf"\b{age}-year-old\b|\b(?:is|of|aged)\s+{age}\b")
    head = re.split(r"[:.]", low, maxsplit=1)[0]
    subject = re.search(r"\bof\s+(?:a|an|the)\s+(.+)$", head)
    if not keys:
        words = (subject.group(1) if subject else head).split()
        if re.fullmatch(r"(?:prop|object)?\s*close-up|prop|object", head.strip()):
            words = low.split(":", 1)[1].split(".")[0].split()[:8] if ":" in low else words
        stop = {"a", "an", "the", "of", "on", "at", "in", "with", "and", "small", "large", "old-fashioned", "hand-made",
                "black", "white", "red", "night", "day", "character", "reference", "sheet", "prop", "close-up"}
        words = [w.strip(",;") for w in words if w.strip(",;") not in stop]
        keys += [rf"\b{re.escape(a)}\s+{re.escape(b)}\b" for a, b in zip(words, words[1:])][:4]
    elif subject and not re.match(r"\d", subject.group(1)):
        keys.append(rf"\b{re.escape(subject.group(1).split(':')[0].strip())}\b")
    return keys


def _parse_plain_scenes(path: Path, text: str) -> tuple[list[dict], list[dict]]:
    scenes = [{"id": m.group(1).upper(), "heading": "", "voice_hint": "", "prompt": _paragraph(text, m.end()),
               "chapter": "", "preview": False} for m in SCENE_LINE.finditer(text)]
    if not scenes:
        raise UserError(f"В файле {path.name} не найдены сцены SCENE S001, SCENE S002…")
    refs = []
    for m in REF_LINE.finditer(text):
        prompt = _paragraph(text, m.end())
        refs.append({"id": m.group(1).upper(), "name": re.split(r"[:.]", prompt, maxsplit=1)[0][:80],
                     "prompt": prompt, "keys": _plain_ref_keys(prompt)})
    return scenes, refs


def _parse_scenes(path: Path) -> tuple[list[dict], list[dict]]:
    text = path.read_text("utf-8", errors="replace")
    matches = list(SCENE_HEAD.finditer(text))
    if not matches and SCENE_LINE.search(text):
        return _parse_plain_scenes(path, text)
    if not matches:
        raise UserError(f"В файле {path.name} не найдены сцены S001, S002…")
    chapters = list(re.finditer(r"(?m)^##\s+([^#\n].*)$", text))
    scenes = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[match.end():end]
        raw_heading = match.group(2).strip()
        voice = re.search(r"(?m)^Голос:\s*(.+)$", block)
        before = [h for h in chapters if h.start() < match.start()]
        chapter_raw = before[-1].group(1).strip() if before else ""
        scenes.append({
            "id": match.group(1).upper(),
            "heading": raw_heading.lstrip("· ").strip(),
            "voice_hint": voice.group(1).strip() if voice else "",
            "prompt": _extract_code(block, match.group(1)),
            "chapter": _chapter(chapter_raw) if "КАРТОЧ" in raw_heading.upper() else "",
            "preview": "ПРЕВЬЮ" in (voice.group(1).upper() if voice else ""),
        })
    refs = []
    ref_matches = list(REF_HEAD.finditer(text))
    first_scene = matches[0].start()
    for i, match in enumerate(ref_matches):
        end = ref_matches[i + 1].start() if i + 1 < len(ref_matches) else first_scene
        if match.start() > first_scene:
            continue
        refs.append({"id": match.group(1).upper(), "name": match.group(2).lstrip("· ").strip(),
                     "prompt": _extract_code(text[match.end():end], match.group(1))})
    return scenes, refs


def _find_scene_file(root: Path) -> Path:
    candidates = []
    for path in [*root.rglob("*.md"), *root.rglob("*.txt")]:
        if any(part.startswith(".") or part.lower() in ("готово", "output", "render", "__macosx")
               for part in path.relative_to(root).parts[:-1]) or path.name.startswith("."):
            continue
        try:
            text = path.read_text("utf-8", errors="ignore")
            count = len(SCENE_HEAD.findall(text)) or len(SCENE_LINE.findall(text))
        except OSError:
            continue
        if count:
            rel = path.relative_to(root)
            priority = (3 if "исходники" in (part.lower() for part in rel.parts) else 0) \
                       + (2 if "сцен" in path.stem.lower() or "scene" in path.stem.lower() else 0) \
                       - (2 if any(re.fullmatch(r"[A-Z]{2}", part) for part in rel.parts[:-1]) else 0)
            candidates.append((count, priority, path))
    if not candidates:
        raise UserError("Не найден файл с описаниями сцен S001, S002… (например, «сцены_генерация.txt»).")
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def selected_langs(options: dict) -> list[str]:
    raw = options.get("folder_languages") or "en"
    items = raw if isinstance(raw, (list, tuple)) else re.split(r"[,\s]+", str(raw))
    out = [x.lower() for x in items if str(x).lower() in LANGS]
    return [x for x in LANGS if x in out] or ["en"]


def _lang_dir(root: Path, lang: str) -> Path | None:
    for child in root.iterdir() if root.is_dir() else []:
        if child.is_dir() and child.name.lower() == lang:
            return child
    return None


def _lang_home(root: Path, lang: str) -> Path | None:
    """Where one language's files live: its own folder (EN/, ES/…) or, in the flat layout, the project folder
    itself when files there carry the language in the name (озвучка_ES.txt, загрузка_YouTube_ES.md)."""
    folder = _lang_dir(root, lang)
    if folder:
        return folder
    code = lang.upper()
    if root.is_dir() and any(p.is_file() and re.search(rf"(?<![A-Za-z]){code}(?![A-Za-z])", p.stem)
                             and not p.name.startswith(".") for p in root.iterdir()):
        return root
    return None


def _find_narration(root: Path, lang: str = "en") -> Path | None:
    """The final narration text of one language: <LANG>/…озвучка…<LANG>.txt (or any narration txt in that folder)."""
    folder = _lang_dir(root, lang)
    code = lang.upper()
    candidates = []
    if folder:
        texts = [p for p in folder.glob("*.txt") if not p.name.startswith((".", "_"))]
        named = [p for p in texts if any(w in p.name.lower() for w in NARRATION_WORDS)]
        coded = [p for p in named if re.search(rf"(?<![A-Za-z]){code}(?![A-Za-z])", p.stem)]
        candidates = coded or named or (texts if len(texts) == 1 else [])
    if not candidates and not folder:
        # Flat layout: everything lies in the project folder itself, the language only in the name (озвучка_ES.txt).
        places = [root] + [p for p in root.iterdir() if p.is_dir() and p.name.lower() == "исходники"]
        texts = [p for d in places for p in d.glob("*.txt") if not p.name.startswith((".", "_"))]
        candidates = [p for p in texts if any(w in p.name.lower() for w in NARRATION_WORDS)
                      and re.search(rf"(?<![A-Za-z]){code}(?![A-Za-z])", p.stem)]
    if not candidates and lang == "en":
        # Old layout: the English narration somewhere else in the project, never another language's file.
        others = [x.upper() for x in LANGS if x != "en"]
        candidates = [p for p in root.rglob("*.txt") if "озвуч" in p.name.lower()
                      and not any(re.search(rf"(?<![A-Za-z]){o}(?![A-Za-z])", p.stem) for o in others)
                      and not any(part.upper() in others for part in p.relative_to(root).parts[:-1])]
    if not candidates:
        return None
    return min(candidates, key=lambda p: (len(p.parts), len(p.name)))


def _scan_media(root: Path, lang: str = "en") -> dict[str, Path]:
    """S001… files of the project. Files inside another language's folder (DE/S005.png with German text on it)
    belong only to that language; a file in this language's folder wins over the shared one."""
    found: dict[str, Path] = {}
    rank: dict[str, tuple] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXT | VIDEO_EXT:
            continue
        rel = path.relative_to(root)
        if any(part.startswith((".", "_")) or part.lower() in ("готово", "output", "render") for part in rel.parts[:-1]):
            continue
        codes = [part.lower() for part in rel.parts[:-1] if re.fullmatch(r"[A-Za-z]{2}", part)]
        if any(code != lang for code in codes):
            continue
        match = SCENE_FILE.match(path.name)
        if not match:
            continue
        sid = f"S{int(match.group(1)):03d}"
        score = (path.suffix.lower() in VIDEO_EXT, bool(codes))
        if sid not in found or score > rank[sid]:
            found[sid], rank[sid] = path, score
    return found


def _analyze(path: Path) -> dict:
    path = path.expanduser().resolve()
    if not path.is_dir():
        raise UserError(f"Папка не найдена: {path}")
    scene_file = _find_scene_file(path)
    scenes, refs = _parse_scenes(scene_file)
    languages = {}
    for lang in LANGS:
        narration = _find_narration(path, lang)
        if not narration:
            continue
        folder = _lang_home(path, lang) or (path / lang.upper())
        languages[lang] = {"narration_file": str(narration),
                           "words": len(narration.read_text("utf-8", errors="replace").split()),
                           "output": str(folder / f"{path.name}_{lang.upper()}.mp4")}
    if not languages:
        raise UserError("Не найден текст озвучки ни на одном языке. Положите его в папку EN, RU, DE или ES "
                        "(файл с «озвучка» в названии, например «озвучка_EN.txt»).")
    existing = _scan_media(path)
    videos = sum(existing.get(s["id"], Path("x")).suffix.lower() in VIDEO_EXT for s in scenes if s["id"] in existing)
    pictures = sum(existing.get(s["id"], Path("x")).suffix.lower() in IMAGE_EXT for s in scenes if s["id"] in existing)
    missing = [s["id"] for s in scenes if s["id"] not in existing]
    first = languages.get("en") or next(iter(languages.values()))
    frame_dir = scene_file.parent / "кадры"
    return {
        "path": str(path), "title": path.name, "scene_file": str(scene_file), "narration_file": first["narration_file"],
        "frame_dir": str(frame_dir), "output": first["output"], "scenes": len(scenes), "references": len(refs),
        "existing_videos": videos, "existing_images": pictures, "missing_images": len(missing),
        "missing_ids": missing[:30], "words": first["words"], "languages": languages,
        "hinted_scenes": sum(bool(scene.get("voice_hint")) for scene in scenes),
        "styled_scenes": sum(bool(re.search(r"\bstyle\s*:", scene.get("prompt") or "", re.I)) for scene in scenes),
    }


def _setting_summary(settings: dict) -> dict:
    provider = images.PROVIDERS.get(settings.get("image_provider"), images.PROVIDERS["fal"])
    image_model = settings.get(provider["model_field"]) or provider["models"][0][0]
    llm_model = settings.get("openai_model") if settings.get("llm_provider") == "openai" else settings.get("anthropic_model")
    voice_service = {"royal": "RoyalTechno", "lumean": "Lumean"}.get(settings.get("voice_provider"), "ElevenLabs")
    return {
        "image_provider": provider["name"], "image_model": image_model, "voice_service": voice_service,
        "reference_model": ("Модель выбранного сервиса (Fast Gen)" if images.fastgen_refs(settings)
                            else "Nano Banana Edit (~$0.04 за референсную сцену)" if settings.get("fal_key")
                            else "Модель выбранного сервиса" if images.royal_refs(settings) else "недоступна"),
        "llm_provider": "OpenAI" if settings.get("llm_provider") == "openai" else "Claude",
        "llm_model": llm_model, "voice_id": settings.get("folder_voice_en_id") or settings.get("elevenlabs_voice_id"),
        "voice_name": settings.get("folder_voice_en_name") or settings.get("elevenlabs_voice_name") or "",
        "music_tracks": len(music.tracks()),
    }


def _problems(settings: dict, analysis: dict) -> tuple[list[str], list[str]]:
    problems, warnings = [], []
    provider = images.PROVIDERS.get(settings.get("image_provider"), images.PROVIDERS["fal"])
    needs_images = int(analysis.get("missing_images") or 0)
    try:
        needs_images = int(_generation_estimate(settings, analysis).get("images") or needs_images)
    except Exception:
        pass
    if needs_images and not settings.get(provider["key_field"]):
        problems.append(f"Не указан ключ для {provider['name']}.")
    if scene_binding(settings) == "smart" and not llm.has_key(settings):
        problems.append("Не указан ключ выбранной языковой модели для распределения текста по сценам.")
    if settings.get("voice_provider") == "royal":
        if not settings.get("royal_key"):
            problems.append("Не указан ключ RoyalTechno для озвучки.")
    elif settings.get("voice_provider") == "lumean":
        if not settings.get("lumean_key"):
            problems.append("Не указан ключ Lumean для озвучки (Настройки → «2. Озвучка»).")
    elif not settings.get("elevenlabs_key"):
        problems.append("Не указан ключ ElevenLabs для озвучки.")
    found = analysis.get("languages") or {}
    wanted = selected_langs(settings)
    present = [lang for lang in wanted if lang in found]
    if not present:
        problems.append("В папке нет текста озвучки на выбранных языках ("
                        + ", ".join(LANG_RU[x] for x in wanted) + ").")
    for lang in wanted:
        if lang not in found:
            warnings.append(f"Нет текста озвучки на языке «{LANG_RU[lang]}» (папка {lang.upper()}): этот язык пропущу.")
    for lang in present:
        if not settings.get(f"folder_voice_{lang}_id") and not (lang == "en" and settings.get("elevenlabs_voice_id")):
            problems.append(f"Не выбран голос для языка «{LANG_RU[lang]}».")
    if scene_binding(settings) == "hints" and analysis.get("hinted_scenes") == 0:
        warnings.append("В файле сцен нет подсказок «Голос:»: сцены разложу равномерно по предложениям текста. "
                        "Чтобы кадр точнее совпадал с тем, о чём говорит голос, выберите «ИИ уточняет» в «Привязке сцен».")
    elif scene_binding(settings) == "even":
        warnings.append("Сцены раскладываются равномерно по тексту, без подсказок «Голос:»: "
                        "кадр может не совпасть с тем, о чём говорит голос.")
    elif "en" not in found and any(lang != "en" for lang in present) and scene_binding(settings) == "hints":
        warnings.append("Нет английского текста: сцены других языков будут привязаны по подсказкам «Голос:» "
                        "и пропорционально. Для точной привязки выберите «ИИ уточняет» в «Привязке сцен».")
    if settings.get("folder_upload"):
        if not settings.get("octo_token") and not settings.get("octo_local_profiles"):
            problems.append("Автовыкладка включена, но профили Octo не заданы: вставьте API-токен или найдите "
                            "открытые профили кнопкой «Найти открытые профили» (без токена).")
        rules = settings.get("octo_rules") or {}
        channel = Path(analysis["path"]).parent.name
        for lang in present:
            if not rules.get(f"{channel}|{lang}"):
                warnings.append(f"Не выбран профиль Octo для «{channel} · {lang.upper()}»: ролик соберу, но не выложу.")
            folder = _lang_home(Path(analysis["path"]), lang)
            if not folder or not upload_package.find_file(folder, lang):
                warnings.append(f"Нет файла загрузки для {lang.upper()} (загрузка_YouTube_{lang.upper()}.md): "
                                "ролик соберу, но не выложу.")
    if settings.get("folder_motion_inserts", True) and not llm.has_key(settings):
        warnings.append("Карты и линии времени выключены: для них нужен ключ Claude или OpenAI (Настройки → «1. Сценарий»).")
    if settings.get("folder_references", True) and analysis.get("references") and not images.can_reference(settings):
        warnings.append("Референсы включены, но выбранный сервис не умеет рисовать по образцу: обычные сцены будут созданы по тексту.")
    brief = _music_brief(analysis["path"]) if analysis.get("path") else None
    if brief and settings.get("folder_music", True):
        warnings.append(f"Музыку сочиню по описанию из папки ({Path(brief['file']).name}): {brief['style_ru']}.")
    if _wants_compose(settings, analysis.get("path") or "."):
        who = music.provider(settings)
        if who == "lumean" and not settings.get("lumean_key") or who == "elevenlabs" and not settings.get("elevenlabs_key"):
            warnings.append("Для сочинения музыки нужен ключ Lumean или ElevenLabs: возьму трек из библиотеки.")
    elif settings.get("folder_music", True) and not music.tracks():
        warnings.append("Библиотека музыки пуста: ролик будет собран без фоновой музыки.")
    if analysis.get("styled_scenes", 0) < analysis.get("scenes", 0):
        count = analysis.get("scenes", 0) - analysis.get("styled_scenes", 0)
        warnings.append(f"У {count} сцен не найден свой Style: к ним будет добавлен общий стиль из Настроек.")
    return problems, warnings


def _public_job(job: dict) -> dict:
    out = {k: v for k, v in job.items() if not k.startswith("_")}
    try:
        active = ("running", "queued", "pausing", "stopping")
        analysis = _analyze(Path(job["path"])) if job.get("status") not in active else job.get("analysis", {})
        out["analysis"] = analysis
        if job.get("status") not in active:
            job["analysis"] = analysis
    except Exception as exc:
        out["analysis"] = job.get("analysis", {})
        out["scan_error"] = str(exc)
    return out


@router.get("")
def list_queue(channel: str = ""):
    settings = load_settings()
    channel = folder_inbox._nfc(channel).strip()
    with _lock:
        jobs = [_public_job(job) for job in sorted(_jobs.values(), key=lambda x: x.get("added", 0))]
    for job in jobs:
        if job.get("analysis"):
            job["channel"] = channel_of(job.get("path"))
            chosen = {**channel_settings(settings, job["channel"]), **(job.get("options") or {})}
            try:
                job["problems"], job["warnings"] = _problems(chosen, job["analysis"])
            except Exception as exc:  # one broken project must not take the whole queue page down
                job["problems"], job["warnings"] = [f"Не получилось проверить папку проекта: {exc}"], []
            try:
                job["estimate"] = _generation_estimate(chosen, job["analysis"])
            except Exception:
                job["estimate"] = {}
    shown = channel_settings(settings, channel)
    return {"jobs": jobs, "settings": {k: shown.get(k) for k in FOLDER_OPTION_KEYS}, "inbox": _inbox_public(),
        "summary": _setting_summary(settings), "octo_token_set": bool(settings.get("octo_token")),
        "channel": channel, "channel_custom": bool(channel and channel_custom(settings, channel)),
        "channel_parent": channel_parent(settings, channel) if channel else "",
        "channel_work": _channel_work_public(settings, channel),
        "channels": known_channels(settings)}


def _channel_work_public(settings: dict, channel: str) -> dict:
    if not channel:
        return {}
    path, owner = channel_work_dir(settings, channel)
    own = str((settings.get("folder_channel_work_dirs") or {}).get(channel) or "")
    return {"own": own, "path": path, "from": owner, "family": channel_family(channel)}


def _work_dir_problem(path: str, inbox: str) -> str:
    try:
        work_r, inbox_r = Path(path).expanduser().resolve(), Path(inbox).expanduser().resolve() if inbox else None
    except OSError as exc:
        return f"Не открывается папка: {exc}"
    if not Path(path).expanduser().is_absolute():
        return "Нужен полный путь к папке (лучше выбрать кнопкой «📁 Выбрать»)."
    if inbox_r and (work_r == inbox_r or inbox_r in work_r.parents or work_r in inbox_r.parents):
        return "Рабочая папка не должна быть внутри папки входящих (и наоборот). Выберите другую."
    return ""


@router.post("/channel/workdir")
def set_channel_work_dir(data: dict = Body(...)):
    """The channel's own work folder; an empty path returns the channel to its family's or the common folder."""
    name = folder_inbox._nfc(str(data.get("channel") or "")).strip()
    if not name:
        raise UserError("Сначала выберите канал.")
    path = str(data.get("path") or "").strip()
    settings = load_settings()
    if path:
        problem = _work_dir_problem(path, str(settings.get("folder_inbox_dir") or "").strip())
        if problem:
            raise UserError(problem)
    dirs = dict(settings.get("folder_channel_work_dirs") or {})
    if path:
        dirs[name] = path
    else:
        dirs.pop(name, None)
    save_settings({"folder_channel_work_dirs": dirs})
    _inbox_wake.set()
    return {"ok": True, "channel_work": _channel_work_public(load_settings(), name)}


@router.post("/settings")
def update_folder_settings(data: dict = Body(...)):
    """Persist controls before the queue UI is redrawn or the app is closed."""
    channel = folder_inbox._nfc(str(data.get("channel") or "")).strip()
    _save_options(data.get("options") or {}, channel, bool(data.get("own", True)))
    settings = channel_settings(load_settings(), channel)
    return {"ok": True, "settings": {k: settings.get(k) for k in FOLDER_OPTION_KEYS}}


@router.post("/channel/add")
def add_channel(data: dict = Body(...)):
    name = folder_inbox._nfc(str(data.get("name") or "")).strip()
    if not name or name.startswith(("_", ".")) or any(ch in name for ch in '/\\:'):
        raise UserError("Название канала — как имя папки: без «/», «\\», «:» и не с «_» или точки в начале.")
    settings = load_settings()
    extra = list(settings.get("folder_channels_extra") or [])
    if name not in extra:
        extra.append(name)
    save_settings({"folder_channels_extra": extra})
    return {"ok": True, "channels": known_channels(load_settings())}


@router.post("/channel/reset")
def reset_channel(data: dict = Body(...)):
    """The channel goes back to the common settings (its Octo profiles stay)."""
    name = folder_inbox._nfc(str(data.get("channel") or "")).strip()
    stored = dict(load_settings().get("folder_channel_settings") or {})
    stored.pop(name, None)
    save_settings({"folder_channel_settings": stored})
    return {"ok": True}


@router.post("/pick")
def pick_folders():
    if sys.platform != "darwin":
        raise UserError("Выбор папки кнопкой пока поддерживается на Mac.")
    script = """
set chosenFolders to choose folder with prompt "Выберите папки проектов для автосборки" with multiple selections allowed
set answer to ""
repeat with oneFolder in chosenFolders
  set answer to answer & POSIX path of oneFolder & linefeed
end repeat
return answer
"""
    try:
        run = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UserError(f"Не получилось открыть выбор папки: {exc}") from exc
    if run.returncode:
        if "User canceled" in run.stderr or "-128" in run.stderr:
            return {"paths": []}
        raise UserError(f"Не получилось выбрать папку: {run.stderr.strip()[-300:]}")
    return {"paths": [line.strip().rstrip("/") for line in run.stdout.splitlines() if line.strip()]}


@router.post("/pick-one")
def pick_one_folder(data: dict = Body(default={})):
    """One folder for the inbox/work settings."""
    if sys.platform != "darwin":
        raise UserError("Выбор папки кнопкой пока поддерживается на Mac. Впишите путь в поле вручную.")
    prompt = str(data.get("prompt") or "Выберите папку").replace('"', "'")
    script = f'return POSIX path of (choose folder with prompt "{prompt}")'
    try:
        run = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UserError(f"Не получилось открыть выбор папки: {exc}") from exc
    if run.returncode:
        if "User canceled" in run.stderr or "-128" in run.stderr:
            return {"path": ""}
        raise UserError(f"Не получилось выбрать папку: {run.stderr.strip()[-300:]}")
    return {"path": run.stdout.strip().rstrip("/")}


def _add_paths(paths) -> list[str]:
    added = []
    for raw in paths or []:
        analysis = _analyze(Path(str(raw)))
        jid = _job_id(Path(analysis["path"]))
        with _lock:
            old = _jobs.get(jid, {})
            _jobs[jid] = {**old, "id": jid, "path": analysis["path"], "title": analysis["title"],
                          "analysis": analysis, "status": old.get("status") if old.get("status") == "running" else "ready",
                          "progress": old.get("progress", 0), "step": "", "error": "", "added": old.get("added", _now()),
                          "updated": _now(), "output": old.get("output", "")}
            added.append(jid)
    _save_queue()
    return added


@router.post("/add")
def add_folders(data: dict = Body(...)):
    return {"added": _add_paths(data.get("paths"))}


@router.delete("/{job_id}")
def remove_job(job_id: str):
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            raise UserError("Проект не найден в очереди.")
        if job.get("status") in ("running", "queued", "pausing", "stopping"):
            raise UserError("Сначала остановите проект.")
        _jobs.pop(job_id, None)
        _save_queue()
    freed = 0
    cache = ROOT / job_id
    locals_ = [local_editor.ROOT / _project_id(str(job.get("path") or ""), lang) for lang in LANGS]
    for path in (cache, *locals_):
        if not path.is_dir():
            continue
        for base, _dirs, files in os.walk(path, followlinks=False):
            for name in files:
                try:
                    freed += (Path(base) / name).stat().st_size
                except OSError:
                    pass
        shutil.rmtree(path, ignore_errors=True)
    frame_raw = str((job.get("analysis") or {}).get("frame_dir") or "")
    frame_dir = Path(frame_raw) if frame_raw else None
    if frame_dir and frame_dir.is_dir():
        shutil.rmtree(frame_dir / "референсы", ignore_errors=True)
        for leftover in [*frame_dir.glob("S*.raw"), *frame_dir.glob("S*.tmp.png")]:
            leftover.unlink(missing_ok=True)
    return {"ok": True, "bytes_freed": freed}


@router.post("/start")
def start_queue(data: dict = Body(...)):
    _save_options(data.get("options") or {}, str(data.get("channel") or ""), bool(data.get("own", True)))
    settings = load_settings()
    explicit_ids = data.get("ids") is not None
    queued = _queue_jobs(set(data.get("ids") or _jobs), settings, explicit_ids)
    _kick_scheduler()
    return {"queued": queued}


def _queue_jobs(ids: set, settings: dict, explicit_ids: bool) -> list[str]:
    """Same checks as the «Запустить» button: projects with problems get status error with the reason."""
    allowed = set(FOLDER_OPTION_KEYS)
    queued = []
    with _lock:
        for jid, job in _jobs.items():
            if jid not in ids or job.get("status") in ("running", "pausing", "stopping"):
                continue
            if not explicit_ids and job.get("status") == "done":
                continue
            analysis = _analyze(Path(job["path"]))
            chosen = channel_settings(settings, channel_of(job["path"]))
            problems, warnings = _problems(chosen, analysis)
            job["analysis"], job["warnings"] = analysis, warnings
            if problems:
                job.update(status="error", error=" ".join(problems), step="", progress=0)
                continue
            if job.get("status") == "done":
                job["langs_done"] = {}
            job.update(status="queued", error="", step="Ждёт своей очереди…", progress=0, options={k: chosen.get(k) for k in allowed},
                       auto_retries=0, retry_at=0, last_error="")
            queued.append(jid)
        _save_queue()
    return queued


ASK = "ask"  # the channel's rule: the owner picks the Octo profile for every video of that channel


def _missing_picks(job: dict) -> list[str]:
    """Languages whose channel says «выбирать у каждого ролика» and this video has no profile picked yet."""
    rules = load_settings().get("octo_rules") or {}
    channel = Path(job["path"]).parent.name
    langs = list((job.get("thumbs") or {}).keys()) or list(((job.get("analysis") or {}).get("languages") or {}).keys())
    picks = job.get("octo_pick") or {}
    return [lang for lang in langs if rules.get(f"{channel}|{lang}") == ASK and not picks.get(lang)
            and lang not in (job.get("uploaded") or {})]


@router.post("/{job_id}/octo-pick")
def octo_pick(job_id: str, data: dict = Body(...)):
    """This one video goes to this Octo profile (over the channel's profile); empty = as the channel says."""
    lang, uuid = str(data.get("lang") or ""), str(data.get("uuid") or "")
    if lang not in LANGS:
        raise UserError("Не понял, для какого языка профиль.")
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            raise UserError("Проект не найден в очереди.")
        picks = dict(job.get("octo_pick") or {})
        if uuid and uuid != ASK:
            picks[lang] = uuid
        else:
            picks.pop(lang, None)
        job["octo_pick"] = picks
        job["updated"] = _now()
        _save_queue()
    return {"octo_pick": picks}


@router.post("/{job_id}/action")
def control_job(job_id: str, data: dict = Body(...)):
    action = str(data.get("action") or "").lower()
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            raise UserError("Проект не найден в очереди.")
        status = job.get("status") or "ready"
        if action == "pause":
            if status == "queued":
                job.update(status="paused", step="На паузе", error="")
            elif status == "running":
                job.update(status="pausing", step="Завершаю текущую операцию и ставлю на паузу…", error="")
            elif status not in ("paused", "pausing"):
                raise UserError("Сейчас этот проект нельзя поставить на паузу.")
        elif action == "stop":
            if status in ("queued", "paused"):
                job.update(status="stopped", step="Остановлено", error="")
            elif status in ("running", "pausing"):
                job.update(status="stopping", step="Завершаю текущую операцию и останавливаю…", error="")
            elif status not in ("stopped", "stopping"):
                raise UserError("Сейчас этот проект уже не выполняется.")
        elif action == "publish":
            if status != "review":
                raise UserError("Этот проект сейчас не ждёт проверки превью.")
            missing = _missing_picks(job)
            if missing:
                raise UserError("Выберите, на какой канал выкладывать: " + ", ".join(x.upper() for x in missing)
                                + " (список «Куда выложить этот ролик» в карточке проекта).")
            job["approved"] = {lang: True for lang in (job.get("thumbs") or {})}
            job.update(status="queued", step="Ждёт своей очереди на выкладку…", error="")
        elif action == "upload":
            # The owner's own «Выложить» on a built video: the same upload as the automatic one, also when the
            # «Выкладывать готовые ролики» box is off (a pool of videos made first, published by hand later).
            if status not in ("done", "error", "stopped"):
                raise UserError("Выложить можно собранный ролик, который сейчас не в работе.")
            built = {lang: path for lang, path in (job.get("langs_done") or job.get("outputs") or {}).items()
                     if lang not in (job.get("uploaded") or {}) and Path(str(path)).is_file()}
            if not built:
                raise UserError("Нечего выкладывать: готовых MP4 нет или все языки уже выложены.")
            missing = [lang for lang in _missing_picks(job) if lang in built]
            if missing:
                raise UserError("Выберите, на какой канал выкладывать: " + ", ".join(x.upper() for x in missing)
                                + " (список «Куда выложить этот ролик» в карточке проекта).")
            job["approved"] = {**(job.get("approved") or {}), **{lang: True for lang in built}}
            job.update(force_upload=True, status="queued", step="Ждёт своей очереди на выкладку…", error="",
                       auto_retries=0, retry_at=0, last_error="")
        elif action == "redraw":
            if status not in ("review", "error", "done"):
                raise UserError("Перерисовать превью можно, когда проект не собирается.")
            root = Path(job["path"])
            if not root.is_dir():
                raise UserError(f"Папка проекта не найдена: {root}. Верните её на место или удалите проект из очереди.")
            for path in (job.get("thumbs_generated") or {}).values():
                Path(path).unlink(missing_ok=True)
            source = next((p for p in root.iterdir() if p.is_dir() and p.name.lower() == "исходники"), root)
            for base in source.glob("превью_основа*"):
                base.unlink(missing_ok=True)
            job.update(thumbs_generated={}, thumbs={}, approved={}, status="queued",
                       step="Перерисовываю превью…", error="")
        elif action == "resume":
            if status not in ("paused", "stopped", "error"):
                raise UserError("Этот проект сейчас нельзя продолжить.")
            job.update(status="queued", step="Ждёт своей очереди…", error="", auto_retries=0, retry_at=0, last_error="")
        else:
            raise UserError("Неизвестная команда управления проектом.")
        job["updated"] = _now()
        _save_queue()
    if action in ("stop", "resume", "publish", "redraw", "upload"):
        _kick_scheduler()
    return {"ok": True, "status": job.get("status")}


_profiles_cache: dict = {"at": 0, "items": []}


@router.get("/octo/profiles")
def octo_profiles(refresh: bool = False):
    settings = load_settings()
    if refresh or time.time() - _profiles_cache["at"] > 300 or not _profiles_cache["items"]:
        _profiles_cache.update(at=time.time(), items=octo.profiles(settings))
    return {"profiles": _profiles_cache["items"], "rules": settings.get("octo_rules") or {},
            "keep_open": settings.get("octo_keep_open") or [],
            "yt_channels": settings.get("octo_yt_channels") or {}}


def _local_list() -> list[dict]:
    return [dict(p) for p in load_settings().get("octo_local_profiles") or [] if isinstance(p, dict) and p.get("uuid")]


def _save_local(items: list[dict]) -> dict:
    save_settings({"octo_local_profiles": items})
    _profiles_cache.update(at=0, items=[])
    settings = load_settings()
    return {"profiles": octo.profiles(settings), "rules": settings.get("octo_rules") or {},
            "keep_open": settings.get("octo_keep_open") or [],
            "yt_channels": settings.get("octo_yt_channels") or {}}


@router.post("/octo/scan")
def octo_scan():
    """Without the token: remembers the profiles the owner has open in Octo right now."""
    found = octo.scan_open()
    if not found:
        raise UserError("В Octo Browser сейчас нет открытых профилей. Откройте в Octo профиль канала (кнопкой "
                        "запуска) и нажмите «Найти открытые профили» ещё раз.")
    items = _local_list()
    known = {p["uuid"] for p in items}
    added = 0
    for p in found:
        if p["uuid"] not in known:
            items.append({"uuid": p["uuid"], "title": p["title"] or f"Профиль {p['uuid'][:8]}"})
            added += 1
    out = _save_local(items)
    out["added"], out["open"] = added, len(found)
    return out


@router.post("/octo/local-profile")
def octo_local_profile(data: dict = Body(...)):
    """Add (UUID pasted by hand), rename or remove a profile remembered without the token."""
    uuid = re.sub(r"[^0-9a-fA-F-]", "", str(data.get("uuid") or ""))
    if len(uuid.replace("-", "")) < 16:
        raise UserError("Это не похоже на UUID профиля Octo (в карточке профиля: 32 буквы и цифры).")
    items = [p for p in _local_list() if p["uuid"] != uuid] if data.get("remove") else _local_list()
    if not data.get("remove"):
        title = str(data.get("title") or "").strip()[:80]
        cur = next((p for p in items if p["uuid"] == uuid), None)
        if cur:
            cur["title"] = title or cur["title"]
        else:
            items.append({"uuid": uuid, "title": title or f"Профиль {uuid[:8]}"})
    return _save_local(items)


@router.post("/octo/keep")
def octo_keep(data: dict = Body(...)):
    """Profiles the program must not close after uploading."""
    uuid, keep = str(data.get("uuid") or ""), bool(data.get("keep"))
    if not uuid:
        raise UserError("Не понял, какой профиль.")
    items = [u for u in (load_settings().get("octo_keep_open") or []) if u != uuid] + ([uuid] if keep else [])
    save_settings({"octo_keep_open": items})
    return {"keep_open": items}


@router.post("/octo/yt-channel")
def octo_yt_channel(data: dict = Body(...)):
    """The YouTube channel a profile must upload to for this channel+language (an account may own several)."""
    from .studio_upload import channel_ref_of
    channel, lang, uuid = str(data.get("channel") or ""), str(data.get("lang") or ""), str(data.get("uuid") or "")
    text = str(data.get("ref") or "").strip()
    if not channel or lang not in LANGS or not uuid:
        raise UserError("Сначала выберите профиль Octo для этого языка.")
    ref = channel_ref_of(text)
    if text and not ref:
        raise UserError("Не понял адрес канала. Вставьте ссылку на канал (youtube.com/channel/UC… или youtube.com/@имя) "
                        "или его номер UC….")
    items = dict(load_settings().get("octo_yt_channels") or {})
    key = f"{channel}|{lang}|{uuid}"
    if ref:
        items[key] = {"ref": ref, "id": ref if ref.startswith("UC") else "", "name": ""}
    else:
        items.pop(key, None)
    save_settings({"octo_yt_channels": items})
    return {"yt_channels": items}


@router.post("/octo/rule")
def octo_rule(data: dict = Body(...)):
    """Channel (the folder the projects lie in) + language -> Octo profile; set once, used by every folder there."""
    channel, lang, uuid = str(data.get("channel") or ""), str(data.get("lang") or ""), str(data.get("uuid") or "")
    if not channel or lang not in LANGS:
        raise UserError("Не понял, для какого канала и языка профиль.")
    rules = dict(load_settings().get("octo_rules") or {})
    if uuid:
        rules[f"{channel}|{lang}"] = uuid
    else:
        rules.pop(f"{channel}|{lang}", None)
    save_settings({"octo_rules": rules})
    return {"rules": rules}


@router.get("/subtitle-preview")
def subtitle_preview(preset: str = "karaoke", font: str = "auto", size: str = "medium", position: str = "mid_low",
                     job: str = "", lang: str = ""):
    """A frame of the project (its first scene) with a sample subtitle line in the chosen style, size and place."""
    from fastapi.responses import FileResponse
    with _lock:
        jobs = list(_jobs.values())
    picked = next((x for x in jobs if x.get("id") == job), None) or (jobs[0] if jobs else None)
    langs = selected_langs(load_settings())
    lang = lang if lang in LANGS else (langs[0] if langs else "en")
    background = None
    if picked and Path(str(picked.get("path") or "")).is_dir():
        found = _scan_media(Path(picked["path"]), lang)
        stills = [found[k] for k in sorted(found) if found[k].suffix.lower() in IMAGE_EXT]
        background = (stills or [found[k] for k in sorted(found)] or [None])[0]
    out_dir = ROOT / "subtitle-preview"
    key = hashlib.sha256(json.dumps([preset, font, size, position, lang, str(background),
                                     background.stat().st_mtime_ns if background else 0]).encode()).hexdigest()[:16]
    out = out_dir / f"{key}.jpg"
    if not out.is_file():
        if out_dir.exists():
            for old in out_dir.glob("*.jpg"):
                old.unlink(missing_ok=True)
        fast_render.preview(background, out, {"preset": preset, "font": font, "size": size, "position": position},
                            lang, out_dir)
    return FileResponse(out, headers={"Cache-Control": "no-store"})


@router.get("/{job_id}/thumb/{lang}")
def thumb_file(job_id: str, lang: str):
    from fastapi.responses import FileResponse
    path = Path(str(((_jobs.get(job_id) or {}).get("thumbs") or {}).get(lang) or ""))
    if not path.is_file():
        raise UserError("Превью ещё нет.")
    return FileResponse(path, headers={"Cache-Control": "no-store"})


@router.post("/{job_id}/reveal")
def reveal(job_id: str, data: dict = Body(default={})):
    job = _jobs.get(job_id)
    outputs = (job or {}).get("outputs") or {}
    path = Path(str(outputs.get(str(data.get("lang") or "")) or (job or {}).get("output") or ""))
    if not path.is_file():
        raise UserError("Готовый ролик ещё не найден.")
    subprocess.Popen(["open", "-R", str(path)] if sys.platform == "darwin" else ["xdg-open", str(path.parent)])
    return {"ok": True}


def _sentences(text: str) -> list[str]:
    items = [m.group(0).strip() for m in re.finditer(r".+?(?:[.!?…]+[\"”’»)]*(?=\s+|$)|$)", text.strip(), re.S)]
    return [x for x in items if x]


def _tokens(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", text.lower().replace("ё", "е").replace("’", "'"))


def _projected_mapping(en_texts: list[str], en_narration: str, narration: str) -> list[str]:
    """Scenes of another language placed where they stand in the English text: the same paragraph and the same share
    of it (translations keep the paragraphs), the cut moved to the nearest sentence start."""
    def paragraphs(text):
        parts = [p.split() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
        return parts or [text.split()]
    en_pars, tg_pars = paragraphs(en_narration), paragraphs(narration)
    same = len(en_pars) == len(tg_pars) and len(en_pars) > 1
    en_flat = [w for p in en_pars for w in p]
    tg_flat = [w for p in tg_pars for w in p]
    en_starts, acc = [], 0
    for p in en_pars:
        en_starts.append(acc); acc += len(p)
    tg_starts, acc = [], 0
    for p in tg_pars:
        tg_starts.append(acc); acc += len(p)
    sentence_starts = [0] + [i + 1 for i, w in enumerate(tg_flat) if re.search(r"[.!?…][\"”’»)]*$", w)]
    starts, pos = [], 0
    for text in en_texts:
        if same:
            k = max(i for i, st in enumerate(en_starts) if st <= pos) if en_starts else 0
            share = (pos - en_starts[k]) / max(len(en_pars[k]), 1)
            target = tg_starts[k] + round(share * len(tg_pars[k]))
            lo, hi = tg_starts[k], tg_starts[k] + len(tg_pars[k])
        else:
            target = round(pos / max(len(en_flat), 1) * len(tg_flat))
            lo, hi = 0, len(tg_flat)
        near = [x for x in sentence_starts if lo <= x <= hi and abs(x - target) <= max(6, (hi - lo) * .15)]
        starts.append(min(near, key=lambda x: abs(x - target)) if near else target)
        pos += len(text.split())
    starts = [min(len(tg_flat), max(0, x)) for x in starts]
    for i in range(1, len(starts)):
        starts[i] = max(starts[i], starts[i - 1])
    if starts:
        starts[0] = 0
    ends = starts[1:] + [len(tg_flat)]
    return [" ".join(tg_flat[a:b]) for a, b in zip(starts, ends)]


def _fast_mapping(scenes: list[dict], narration: str) -> tuple[list[str], int]:
    """Map scene starts to the exact `Голос:` fragments locally; no model or transcription call."""
    raw_words = narration.split()
    norm_words, owners = [], []
    for raw_index, word in enumerate(raw_words):
        for token in _tokens(word):
            norm_words.append(token)
            owners.append(raw_index)

    def segments(hint: str) -> list[list[str]]:
        hint = re.sub(r"(?i)\bauthor\s*:\s*", "", hint)
        chunks = re.split(r"(?:…+|\.{2,})", hint)
        result = []
        for chunk in chunks:
            tokens = _tokens(chunk)
            if tokens:
                result.append(tokens)
        return sorted(result, key=len, reverse=True)

    def exact(seq: list[str], start: int) -> int | None:
        if not seq:
            return None
        last = len(norm_words) - len(seq) + 1
        for pos in range(max(start, 0), max(last, 0)):
            if norm_words[pos:pos + len(seq)] == seq:
                return pos
        return None

    starts: list[int | None] = [None] * len(scenes)
    cursor, matched = 0, 0
    for index, scene in enumerate(scenes):
        if scene.get("preview"):
            starts[index] = 0
            continue
        found = None
        for seq in segments(scene.get("voice_hint") or ""):
            found = exact(seq, cursor)
            if found is not None:
                break
            # A changed word should not lose the whole anchor: try distinctive consecutive fragments.
            for width in range(min(6, len(seq)), 2, -1):
                for offset in range(0, len(seq) - width + 1):
                    found = exact(seq[offset:offset + width], cursor)
                    if found is not None:
                        break
                if found is not None:
                    break
            if found is not None:
                break
        if found is not None:
            starts[index] = owners[found]
            cursor = found
            matched += 1

    # Fill scenes without a usable English hint between the neighbouring exact anchors.
    known = [i for i, value in enumerate(starts) if value is not None]
    for index in range(len(starts)):
        if starts[index] is not None:
            continue
        before = max((i for i in known if i < index), default=-1)
        after = min((i for i in known if i > index), default=len(starts))
        left = int(starts[before]) if before >= 0 else 0
        right = int(starts[after]) if after < len(starts) else len(raw_words)
        span = max(after - before, 1)
        starts[index] = round(left + (right - left) * (index - before) / span)

    starts = [min(len(raw_words), max(0, int(value or 0))) for value in starts]
    for index in range(1, len(starts)):
        starts[index] = max(starts[index], starts[index - 1])

    # Chapter cards share the opening phrase of the next chapter while the voice continues normally.
    for index, scene in enumerate(scenes[:-1]):
        if not scene.get("chapter"):
            continue
        opening = starts[index + 1]
        following = starts[index + 2] if index + 2 < len(starts) else len(raw_words)
        span = following - opening
        take = min(8, max(5, span // 2), span - 1) if span > 1 else 0
        starts[index] = opening
        starts[index + 1] = min(following, opening + take)
    for index in range(1, len(starts)):
        starts[index] = max(starts[index], starts[index - 1])

    ends = starts[1:] + [len(raw_words)]
    texts = [" ".join(raw_words[start:end]).strip() for start, end in zip(starts, ends)]
    return texts, matched


SCENE_BINDINGS = ("hints", "smart", "even")


def scene_binding(settings: dict) -> str:
    """How scenes are placed on the narration: «Голос:» hints, a model call, or evenly by sentences.
    Older settings only had folder_smart_mapping (true = the model call)."""
    mode = settings.get("folder_scene_binding")
    if mode in SCENE_BINDINGS:
        return mode
    return "smart" if settings.get("folder_smart_mapping", False) else "hints"


def _spread_by_sentences(scenes: list[dict], sentences: list[str], got: list) -> list[str]:
    """Scene texts from sentence boundaries; missing boundaries fall back to an even split."""
    ends, prev, total = [], 0, len(sentences)
    for index, scene in enumerate(scenes):
        fallback = round(total * (index + 1) / max(len(scenes), 1))
        try:
            end = int(got[index] if index < len(got) else fallback)
        except (TypeError, ValueError):
            end = fallback
        if scene.get("preview"):
            end = prev
        else:
            future_spoken = sum(not item.get("preview") for item in scenes[index + 1:])
            minimum = min(prev + 1, total)
            maximum = max(minimum, total - future_spoken)
            end = min(max(end, minimum), maximum)
        ends.append(end)
        prev = end
    if ends:
        ends[-1] = total
    texts, start = [], 0
    for end in ends:
        texts.append(" ".join(sentences[start:end]).strip())
        start = end
    return texts


def _even_mapping(scenes: list[dict], narration: str) -> list[str]:
    """Scenes spread evenly over the narration, cutting on sentence ends, with no hints and no model call."""
    sentences = _sentences(narration)
    spoken = sum(not scene.get("preview") for scene in scenes)
    if len(sentences) >= spoken:
        return _spread_by_sentences(scenes, sentences, [])
    # Fewer sentences than pictures: split by words instead, so no picture is left without voice.
    words = narration.split()
    texts, done, seen = [], 0, 0
    for scene in scenes:
        if scene.get("preview"):
            texts.append("")
            continue
        seen += 1
        end = len(words) if seen == spoken else round(len(words) * seen / max(spoken, 1))
        texts.append(" ".join(words[done:end]).strip())
        done = max(done, end)
    return texts


def _mapping(job_id: str, scenes: list[dict], narration: str, settings: dict, cache: Path,
             english: str = "", progress: float = .04) -> list[str]:
    """english: the English narration of the same project, to place the scenes of another language."""
    mode = scene_binding(settings)
    smart = mode == "smart"
    sentences = _sentences(narration)
    fingerprint = hashlib.sha256((narration + json.dumps(scenes, ensure_ascii=False, sort_keys=True) + mode
                                  + hashlib.sha256(english.encode()).hexdigest()).encode()).hexdigest()
    saved = _read_json(cache / "mapping.json", {})
    if saved.get("fingerprint") == fingerprint and len(saved.get("texts") or []) == len(scenes):
        return saved["texts"]
    if mode == "even":
        _update(job_id, progress=progress, step="Раскладываю сцены равномерно по тексту…")
        texts = _even_mapping(scenes, narration)
        _write_json(cache / "mapping.json", {"fingerprint": fingerprint, "texts": texts, "mode": "even"})
        return texts
    if not any((s.get("voice_hint") or "").strip() for s in scenes) and (not smart or not llm.has_key(settings)):
        # The plain scene file (сцены_генерация.txt) has no «Голос:» hints: spread by sentences instead of words.
        _update(job_id, progress=progress, step="В файле сцен нет подсказок «Голос:»: раскладываю сцены равномерно по предложениям…")
        texts = _even_mapping(scenes, narration)
        _write_json(cache / "mapping.json", {"fingerprint": fingerprint, "texts": texts, "mode": "even"})
        return texts
    if not smart or not llm.has_key(settings):
        _update(job_id, progress=progress, step="Быстро привязываю сцены к подсказкам «Голос:»…")
        texts, matched = _fast_mapping(scenes, narration)
        mode = "fast"
        hinted = sum(bool((s.get("voice_hint") or "").strip()) and not s.get("preview") for s in scenes) or 1
        if english and english.strip() != narration.strip() and matched < hinted * .5:
            # The «Голос:» hints are in English: place the scenes as they stand in the English text.
            en_texts, _ = _fast_mapping(scenes, english)
            texts, mode = _projected_mapping(en_texts, english, narration), "projected"
        _write_json(cache / "mapping.json", {"fingerprint": fingerprint, "texts": texts, "mode": mode, "matched": matched})
        return texts
    _update(job_id, progress=progress, step="ИИ уточняет распределение текста по сценам…")
    scene_lines = []
    for scene in scenes:
        short_prompt = re.sub(r"\s+", " ", scene.get("prompt") or "")[:260]
        scene_lines.append(f"{scene['id']} | voice hint: {scene.get('voice_hint') or '-'} | picture: {short_prompt}")
    numbered = "\n".join(f"{i + 1}. {text}" for i, text in enumerate(sentences))
    schema = {"type": "object", "properties": {"boundaries": {"type": "array", "items": {"type": "integer"}}}}
    usage = {}
    try:
        result = llm.call_json(
            settings,
            "You are a documentary picture editor. Return only a compact JSON array of integers; do not explain.",
            f"Match {len(sentences)} numbered narration sentences to {len(scenes)} visual scenes in the listed order. "
            f"Return boundaries with EXACTLY {len(scenes)} integers. Integer i is the number of the LAST narration sentence "
            "shown with scene i. Values are non-decreasing, between 0 and the sentence count; the last value equals the "
            "sentence count. Only an explicitly silent preview may repeat the preceding value. A chapter card carries the "
            "first sentence of its chapter. Match meaning, voice hints and picture descriptions. Do not rewrite text.\n\n"
            "SCENES:\n" + "\n".join(scene_lines) + "\n\nNARRATION:\n" + numbered,
            schema, usage, max_tokens=24000,
        )
        got = result.get("boundaries") or []
    except UserError as exc:
        if "лимит длины ответа" not in str(exc):
            raise
        # Scene alignment is helpful, but a reasoning-token limit must not kill a full one-click build.
        texts, matched = _fast_mapping(scenes, narration)
        _update(job_id, step="ИИ исчерпал лимит ответа — использую быстрые подсказки «Голос:»…")
        _write_json(cache / "mapping.json", {"fingerprint": fingerprint, "texts": texts, "mode": "fast-fallback", "matched": matched})
        return texts
    texts = _spread_by_sentences(scenes, sentences, got)
    _write_json(cache / "mapping.json", {"fingerprint": fingerprint, "texts": texts, "usage": usage})
    return texts


def _reference_keywords(ref: dict) -> list[str]:
    name = re.sub(r"[^a-z0-9 ]", " ", ref.get("name", "").lower())
    common = {"reference", "today", "small", "large", "young", "ancient", "modern", "little", "single"}
    return [x for x in name.split() if len(x) >= 4 and x not in common]


def _scene_reference_ids(scene: dict, refs: list[dict]) -> list[str]:
    """Choose references only from explicit R-ids or distinctive subject words, never generic adjectives."""
    prompt = scene.get("prompt") or ""
    low = prompt.lower()
    explicit = {x.upper() for x in re.findall(r"(?i)\bR\d+\b", prompt)}
    chosen = []
    for ref in refs:
        rid = ref["id"]
        if ref.get("keys") is not None:
            if rid in explicit or any(re.search(k, low) for k in ref["keys"]):
                chosen.append(rid)
            continue
        keys = _reference_keywords(ref)
        if rid in explicit or keys and (" ".join(keys) in low or any(re.search(rf"\b{re.escape(k)}\b", low) for k in keys)):
            chosen.append(rid)
    return chosen[:3]


def _legacy_reference_ids(scene: dict, refs: list[dict]) -> list[str]:
    """The v182 matcher, used once to identify already generated frames that were contaminated by it."""
    low = (scene.get("prompt") or "").lower()
    out = []
    for ref in refs:
        name = re.sub(r"[^a-z0-9 ]", " ", ref.get("name", "").lower())
        keys = [x for x in name.split() if len(x) >= 4 and x not in {"reference", "today"}]
        if keys and (" ".join(keys) in low or any(k in low for k in keys)):
            out.append(ref["id"])
    return out[:3]


def _image_prompt(scene: dict, settings: dict) -> str:
    prompt = (scene.get("prompt") or scene.get("voice_hint") or scene["id"]).strip()
    if "style:" not in prompt.lower() and settings.get("image_style"):
        prompt += ". " + str(settings["image_style"]).strip()
    return prompt


def _frame_signature(scene: dict, settings: dict, ref_ids: list[str], refs_on: bool) -> str:
    provider = images.PROVIDERS.get(settings.get("image_provider"), images.PROVIDERS["fal"])
    payload = {
        "prompt": _image_prompt(scene, settings), "references": ref_ids if refs_on else [],
        "provider": settings.get("image_provider") or "fal", "model": settings.get(provider["model_field"]),
        "ref_model": settings.get("ref_model") if ref_ids and refs_on else "", "refs_active": bool(ref_ids and refs_on),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _generation_estimate(settings: dict, analysis: dict) -> dict:
    scenes, refs = _parse_scenes(Path(analysis["scene_file"]))
    root, frame_dir = Path(analysis["path"]), Path(analysis["frame_dir"])
    media_map = _scan_media(root)
    manifest = _read_json(frame_dir / ".autoyoutube-generated.json", {})
    frame_manifest = manifest.get("frames", {}) if isinstance(manifest, dict) else {}
    refs_on = bool(settings.get("folder_references", True))
    needed, ref_scenes, required_refs = [], 0, set()
    for scene in scenes:
        sid = scene["id"]
        ref_ids = _scene_reference_ids(scene, refs) if refs_on else []
        current = media_map.get(sid)
        stale = current is None
        generated = frame_dir / f"{sid}.png"
        if current is not None and current == generated:
            recorded = frame_manifest.get(sid, {}).get("signature") if isinstance(frame_manifest.get(sid), dict) else None
            expected = _frame_signature(scene, settings, ref_ids, refs_on)
            legacy_ids = _legacy_reference_ids(scene, refs)
            safe_legacy = recorded is None and ((refs_on and legacy_ids == ref_ids) or (not refs_on and not legacy_ids))
            stale = recorded != expected and not safe_legacy
        if stale:
            needed.append(sid)
            ref_scenes += bool(ref_ids and images.can_reference(settings))
            required_refs.update(ref_ids)
    regular = len(needed) - ref_scenes
    regular_unit = images.price_per_image(settings)
    ref_unit = images.price_per_image(settings) if images.royal_refs(settings) else images.REFERENCE_PRICE
    refs_to_make = len(required_refs) if refs_on and images.can_reference(settings) else 0
    cost = regular * regular_unit + ref_scenes * ref_unit + refs_to_make * regular_unit
    return {"images": len(needed), "reference_scenes": ref_scenes, "regular_scenes": regular,
            "references": refs_to_make, "cost": round(cost, 2), "refs_on": refs_on}


# Image retries: a night queue must not stop on one bad picture.
IMAGE_RETRY_PAUSES = (20, 60, 120)    # seconds between attempts of one picture
IMAGE_SECOND_ROUND_WAIT = 5 * 60      # before the failed pictures get a second round
JOB_AUTO_RETRIES = 2                  # a failed project is put back in the queue this many times
JOB_RETRY_WAIT = 15 * 60
_FATAL_HINTS = ("не указан ключ", "ключ не подходит", "неверный ключ", "invalid api key", "unauthorized")


def _fatal(exc: Exception) -> bool:
    """Errors a retry cannot fix (no key, wrong key): fail at once instead of waiting."""
    low = str(exc).lower()
    return any(h in low for h in _FATAL_HINTS)


def _sleep_checked(job_id: str, seconds: float):
    """Wait, but still react to Pause and Stop within a second."""
    end = time.time() + seconds
    while time.time() < end:
        _check_control(job_id)
        time.sleep(min(1.0, max(end - time.time(), 0)))


def _with_retries(job_id: str, attempts: list, label: str, set_step=None):
    """attempts: callables tried in order with pauses between them. Returns (result, index of the one that worked)."""
    last = None
    for index, call in enumerate(attempts):
        _check_control(job_id)
        try:
            return call(), index
        except (_PauseRequested, _StopRequested):
            raise
        except Exception as exc:  # noqa: BLE001 - every provider error is worth another try
            last = exc
            if _fatal(exc) or index == len(attempts) - 1:
                break
            pause = IMAGE_RETRY_PAUSES[min(index, len(IMAGE_RETRY_PAUSES) - 1)]
            if set_step:
                set_step(f"{label}: ошибка, повторяю через {pause} с (попытка {index + 2} из {len(attempts)})…")
            _sleep_checked(job_id, pause)
    raise last


# Pictures drawn at once, for all projects together (two projects must not overrun the plan's threads).
IMAGE_PARALLEL_MAX = 32
AUTO_IMAGE_PARALLEL = {"fastgen": 10, "fal": 10, "runware": 16, "replicate": 16, "together": 12, "openai": 8, "royal": 10}


def image_workers(settings: dict, options: dict | None = None) -> tuple[int, str]:
    """(how many pictures at once, how it was chosen) for «Сборка из папок»."""
    options = options or {}
    raw = str(options.get("folder_image_parallel") or settings.get("folder_image_parallel") or "auto").strip().lower()
    if raw not in ("", "auto", "0"):
        try:
            return max(1, min(int(float(raw)), IMAGE_PARALLEL_MAX)), "вручную"
        except ValueError:
            pass
    provider = settings.get("image_provider") or "fal"
    if provider == "fastgen" or images.fastgen_refs(settings):
        from . import fastgen
        threads = fastgen.plan_threads(settings)
        if threads:
            return max(1, min(threads, IMAGE_PARALLEL_MAX)), "по тарифу Fast Gen"
        return AUTO_IMAGE_PARALLEL["fastgen"], "Fast Gen: 10 потоков"
    return AUTO_IMAGE_PARALLEL.get(provider, 8), "авто"


class _Gate:
    """A semaphore whose size can change between pictures."""

    def __init__(self):
        self._cond = threading.Condition()
        self._busy = 0
        self.limit = 4

    def run(self, call, limit: int):
        with self._cond:
            self.limit = limit
            self._cond.notify_all()
            while self._busy >= self.limit:
                self._cond.wait(1)
            self._busy += 1
        try:
            return call()
        finally:
            with self._cond:
                self._busy -= 1
                self._cond.notify_all()


_image_gate = _Gate()


def _make_images(job_id: str, analysis: dict, scenes: list[dict], refs: list[dict], settings: dict, options: dict) -> dict[str, Path]:
    _check_control(job_id)
    root = Path(analysis["path"])
    frame_dir = Path(analysis["frame_dir"])
    frame_dir.mkdir(parents=True, exist_ok=True)
    for leftover in [*frame_dir.glob("S*.raw"), *frame_dir.glob("S*.tmp.png")]:
        leftover.unlink(missing_ok=True)
    media_map = _scan_media(root)
    use_refs = bool(options.get("folder_references", True))
    manifest_path = frame_dir / ".autoyoutube-generated.json"
    manifest = _read_json(manifest_path, {})
    if not isinstance(manifest, dict):
        manifest = {}
    frame_manifest = manifest.setdefault("frames", {})
    ref_manifest = manifest.setdefault("references", {})
    ref_paths: dict[str, Path] = {}
    missing = []
    for scene in scenes:
        sid = scene["id"]
        current = media_map.get(sid)
        generated = frame_dir / f"{sid}.png"
        ref_ids = _scene_reference_ids(scene, refs) if use_refs else []
        signature = _frame_signature(scene, settings, ref_ids, use_refs)
        if current is None:
            missing.append(scene)
            continue
        try:
            is_generated = current.resolve() == generated.resolve()
        except OSError:
            is_generated = current == generated
        if not is_generated:
            continue  # owner's video or image always wins and is never regenerated
        recorded = frame_manifest.get(sid, {}).get("signature") if isinstance(frame_manifest.get(sid), dict) else None
        if recorded == signature:
            continue
        legacy_ids = _legacy_reference_ids(scene, refs)
        if recorded is None and ((use_refs and legacy_ids == ref_ids) or (not use_refs and not legacy_ids)):
            # Safe migration: a v182 frame whose reference choice was already correct can stay.
            frame_manifest[sid] = {"signature": signature, "references": ref_ids, "legacy": True}
            continue
        media_map.pop(sid, None)
        missing.append(scene)
    _write_json(manifest_path, manifest)
    if not missing:
        return media_map
    notes: list[str] = []
    workers, how = image_workers(settings, options)
    _update(job_id, image_notes=[])
    required_refs = {rid for scene in missing for rid in (_scene_reference_ids(scene, refs) if use_refs else [])}
    if required_refs and images.can_reference(settings):
        ref_dir = frame_dir / "референсы"
        ref_dir.mkdir(parents=True, exist_ok=True)
        selected_refs = [ref for ref in refs if ref["id"] in required_refs]
        provider = images.PROVIDERS.get(settings.get("image_provider"), images.PROVIDERS["fal"])
        to_draw = []
        for ref in selected_refs:
            out = ref_dir / f"{ref['id']}.png"
            ref_sig = hashlib.sha256(json.dumps([ref.get("prompt"), settings.get("image_provider"),
                settings.get(provider["model_field"])], ensure_ascii=False).encode()).hexdigest()
            if not out.exists() or ref_manifest.get(ref["id"]) != ref_sig:
                to_draw.append((ref, out, ref_sig))
            else:
                ref_paths[ref["id"]] = out
        ref_done = [0]

        def draw_ref(item):
            ref, out, ref_sig = item
            _check_control(job_id)
            ref_prompt = ref.get("prompt") or ref.get("name")
            call = lambda p=ref_prompt: _image_gate.run(lambda: images.generate(settings, p, False), workers)  # noqa: E731
            raw, _ = _with_retries(job_id, [call] * 4, f"Референс {ref['id']}")
            media.to_png(raw, out)
            with _lock:
                ref_manifest[ref["id"]] = ref_sig
                _write_json(manifest_path, manifest)
            return ref, out

        if to_draw:
            _update(job_id, progress=.06, step=f"Создаю референсы: 0 из {len(to_draw)} (одновременно до {workers})…")
            with ThreadPoolExecutor(max_workers=min(workers, len(to_draw)), thread_name_prefix=f"folder-refs-{job_id}") as pool:
                futures = {pool.submit(draw_ref, item): item[0] for item in to_draw}
                try:
                    for future in as_completed(futures):
                        ref = futures[future]
                        try:
                            _ref, out = future.result()
                        except (_PauseRequested, _StopRequested):
                            raise
                        except Exception as exc:  # noqa: BLE001 - scenes are drawn without this reference instead
                            if _fatal(exc):
                                raise
                            notes.append(f"Референс {ref['id']} не нарисовался ({str(exc)[:120]}): его сцены нарисованы без него.")
                            _update(job_id, image_notes=list(notes))
                            continue
                        ref_paths[ref["id"]] = out
                        ref_done[0] += 1
                        _update(job_id, progress=.06 + .04 * ref_done[0] / len(to_draw),
                                step=f"Создаю референсы: {ref_done[0]} из {len(to_draw)} (одновременно до {workers})…")
                except BaseException:
                    for future in futures:
                        future.cancel()
                    raise
            _check_control(job_id)
    done = [0]

    def work(scene: dict, rounds: int = 4):
        _check_control(job_id)
        out = frame_dir / f"{scene['id']}.png"
        prompt = _image_prompt(scene, settings)
        ref_ids = _scene_reference_ids(scene, refs) if use_refs else []
        selected = [ref_paths[rid] for rid in ref_ids if rid in ref_paths] if images.can_reference(settings) else []
        text_only = lambda: _image_gate.run(lambda: images.generate(settings, prompt, False), workers)  # noqa: E731
        if selected:
            # Two tries with the references, then text only: a reference edit can reject an otherwise valid scene.
            with_refs = lambda: _image_gate.run(  # noqa: E731
                lambda: images.generate_with_reference(settings, prompt, selected, False), workers)
            attempts = [with_refs] * (rounds // 2) + [text_only] * (rounds - rounds // 2)
        else:
            attempts = [text_only] * rounds
        raw, used = _with_retries(job_id, attempts, f"Сцена {scene['id']}")
        fallback = bool(selected) and used >= rounds // 2
        media.to_png(raw, out)
        signature = _frame_signature(scene, settings, ref_ids, use_refs)
        with _lock:
            frame_manifest[scene["id"]] = {"signature": signature, "references": ref_ids,
                                             "text_only_fallback": fallback, "updated": _now()}
            _write_json(manifest_path, manifest)
        _check_control(job_id)
        return scene["id"], out

    _update(job_id, step=f"Создаю недостающие изображения: 0 из {len(missing)} (одновременно до {workers}, {how})…")
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"folder-images-{job_id}") as pool:
        futures = {pool.submit(work, scene): scene for scene in missing}
        failures = []
        try:
            for future in as_completed(futures):
                scene = futures[future]
                try:
                    sid, out = future.result()
                except (_PauseRequested, _StopRequested):
                    raise
                except Exception as exc:
                    failures.append(f"{scene['id']}: {exc}")
                    continue
                media_map[sid] = out
                done[0] += 1
                _update(job_id, progress=.10 + .38 * done[0] / len(missing),
                        step=f"Создаю недостающие изображения: {done[0]} из {len(missing)} (одновременно до {workers})…")
                _check_control(job_id)
        except (_PauseRequested, _StopRequested):
            for future in futures:
                future.cancel()
            raise
    if not failures:
        return media_map
    fatal = next((f for f in failures if _fatal(Exception(f))), None)
    if fatal:
        raise UserError(f"Картинки не рисуются: {fatal}")
    # Second round: the service may have been down for a few minutes. Failed scenes wait, then try again one by one.
    by_id = {scene["id"]: scene for scene in missing}
    failed_ids = [f.split(":", 1)[0] for f in failures]
    _update(job_id, step=f"Не нарисовались {len(failed_ids)} сцен. Жду {IMAGE_SECOND_ROUND_WAIT // 60} мин и пробую ещё раз…")
    _sleep_checked(job_id, IMAGE_SECOND_ROUND_WAIT)
    still = []
    for n, sid in enumerate(failed_ids, 1):
        _update(job_id, step=f"Второй круг: сцена {sid} ({n} из {len(failed_ids)})…")
        try:
            sid, out = work(by_id[sid])
            media_map[sid] = out
        except (_PauseRequested, _StopRequested):
            raise
        except Exception as exc:  # noqa: BLE001
            still.append((sid, str(exc)))
    if not still:
        return media_map
    # Last resort: a neighbour's picture stands in, so the video is still built tonight.
    # The stand-in is marked as not generated, and the next «Повторить» draws the scene again.
    order = [scene["id"] for scene in scenes]
    for sid, why in still:
        i = order.index(sid)
        neighbour = next((media_map[x] for x in [*reversed(order[:i]), *order[i + 1:]]
                          if x in media_map and Path(media_map[x]).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")), None)
        if neighbour is None:
            raise UserError(f"Не удалось создать {len(still)} сцен, подменить нечем. {sid}: {why}"[:600])
        out = frame_dir / f"{sid}.png"
        media.to_png(Path(neighbour).read_bytes(), out)
        media_map[sid] = out
        with _lock:
            frame_manifest[sid] = {"signature": "stand-in", "stand_in_for": Path(neighbour).stem, "updated": _now()}
            _write_json(manifest_path, manifest)
        notes.append(f"{sid} не нарисовалась после всех попыток ({why[:120]}). Временно стоит картинка {Path(neighbour).stem}, "
                     f"кнопка «Дорисовать сцены и пересобрать» нарисует её заново.")
    _update(job_id, image_notes=list(notes))
    return media_map


def _voice_chunks(text: str, limit: int = 9000) -> list[str]:
    """Split long narration at natural pauses without exceeding the ElevenLabs request limit."""
    rest = text.strip()
    chunks = []
    while len(rest) > limit:
        window = rest[:limit + 1]
        sentence_breaks = [m.end() for m in re.finditer(r'[.!?…]["”’»)]*\s+', window)]
        useful = [position for position in sentence_breaks if position >= limit * .55]
        if useful:
            cut = useful[-1]
        else:
            spaces = [m.start() for m in re.finditer(r'\s+', window) if m.start() >= limit * .55]
            cut = spaces[-1] if spaces else limit
        chunk = rest[:cut].strip()
        if not chunk:
            chunk, cut = rest[:limit], limit
        chunks.append(chunk)
        rest = rest[cut:].strip()
    if rest:
        chunks.append(rest)
    return chunks


def _make_voice(job_id: str, scenes: list[dict], texts: list[str], narration: str,
                settings: dict, options: dict, cache: Path, lang: str = "en", span: tuple = (.49, .67),
                keep_dir: Path | None = None):
    """Voice the complete narration in the fewest allowed requests, then fit scenes to its word timings.
    keep_dir: the language folder of the project. The voice-over is kept there (озвучка_XX.mp3 plus hidden word
    timings) and taken from there next time, so a rebuild never orders the same narration again."""
    _check_control(job_id)
    audio = cache / "audio"
    audio.mkdir(parents=True, exist_ok=True)
    voice_settings = dict(settings)
    fallback_id = settings.get("elevenlabs_voice_id") if lang == "en" else ""
    voice_settings["elevenlabs_voice_id"] = (options.get(f"folder_voice_{lang}_id") or settings.get(f"folder_voice_{lang}_id")
                                             or fallback_id or settings.get("elevenlabs_voice_id"))
    voice_settings["elevenlabs_voice_name"] = (options.get(f"folder_voice_{lang}_name") or settings.get(f"folder_voice_{lang}_name")
                                               or settings.get("elevenlabs_voice_name"))
    manifest_path = audio / "manifest.json"
    manifest = _read_json(manifest_path, {})
    mp3, spoken = audio / "narration.mp3", audio / "narration.wav"
    voice_values = [
        voice_settings.get("voice_provider"), voice_settings.get("elevenlabs_voice_id"),
        voice_settings.get("elevenlabs_model"), voice_settings.get("voice_speed"),
        voice_settings.get("voice_stability"), voice_settings.get("voice_similarity"),
        voice_settings.get("voice_style"),
    ] + ([voice_settings.get("lumean_model")] if voice_settings.get("voice_provider") == "lumean" else [])
    voice_signature = hashlib.sha256(json.dumps(voice_values, ensure_ascii=False).encode()).hexdigest()
    # Keep the pre-v186 final signature so a valid cached narration is never billed a second time after an update.
    signature = hashlib.sha256(json.dumps([narration, *voice_values], ensure_ascii=False).encode()).hexdigest()
    # Lumean cuts and stitches long text itself: a whole hour of narration goes in one or two orders.
    chunks = _voice_chunks(narration, 45000 if voice_settings.get("voice_provider") == "lumean" else 9000)
    spoken_words = []
    keep_mp3 = keep_dir / f"озвучка_{lang.upper()}.mp3" if keep_dir else None
    keep_json = keep_dir / f".озвучка_{lang.upper()}.json" if keep_dir else None
    kept = _read_json(keep_json, {}) if keep_json else {}
    if (keep_mp3 and keep_mp3.is_file() and kept.get("narration") == signature and kept.get("words")
            and (manifest.get("narration") != signature or not spoken.exists())):
        _update(job_id, step=f"[{lang.upper()}] Беру готовую озвучку из папки проекта…")
        media.to_wav(keep_mp3, spoken)
        spoken_words = [(str(w), float(a), float(b)) for w, a, b in kept["words"]]
        manifest = {"narration": signature, "parts": {}, "chunk_count": 0}
        _write_json(manifest_path, manifest)
    if manifest.get("narration") != signature or not spoken.exists():
        saved_parts = manifest.get("parts") if isinstance(manifest.get("parts"), dict) else {}
        part_wavs = []
        offset = 0.0
        for index, chunk in enumerate(chunks):
            _check_control(job_id)
            number = index + 1
            part_mp3 = audio / f"narration-part-{number:03d}.mp3"
            part_wav = audio / f"narration-part-{number:03d}.wav"
            previous = chunks[index - 1] if index else ""
            following = chunks[index + 1] if index + 1 < len(chunks) else ""
            part_signature = hashlib.sha256(json.dumps([
                voice_signature, chunk, previous[-1000:], following[:1000], lang,
            ], ensure_ascii=False).encode()).hexdigest()
            needs_synthesis = saved_parts.get(str(number)) != part_signature or not part_mp3.exists()
            if needs_synthesis:
                label = "Озвучиваю весь файл…" if len(chunks) == 1 else f"Озвучиваю файл: часть {number} из {len(chunks)}…"
                _update(job_id, progress=span[0] + (span[1] - span[0]) * index / max(len(chunks), 1),
                        step=f"[{lang.upper()}] {label}")
                tts.synthesize(voice_settings, chunk, part_mp3, previous, following, lang)
                saved_parts[str(number)] = part_signature
                manifest = {"parts": saved_parts}
                _write_json(manifest_path, manifest)
            if needs_synthesis or not part_wav.exists():
                media.to_wav(part_mp3, part_wav)
            duration = media.wav_duration(part_wav)
            chunk_words = captions._scene_words(chunk, part_mp3.with_suffix(".json"), duration)
            spoken_words.extend((word, offset + start, offset + end) for word, start, end in chunk_words)
            offset += duration
            part_wavs.append((part_wav, 0.0))
        media.concat_wavs(part_wavs, spoken)
        manifest = {"narration": signature, "parts": saved_parts, "chunk_count": len(chunks)}
        _write_json(manifest_path, manifest)
        _check_control(job_id)
    speech_duration = media.wav_duration(spoken)
    if not spoken_words:
        # Compatibility with a cached one-request narration from an earlier version.
        if manifest.get("chunk_count"):
            offset = 0.0
            for number, chunk in enumerate(chunks, 1):
                part_mp3 = audio / f"narration-part-{number:03d}.mp3"
                part_wav = audio / f"narration-part-{number:03d}.wav"
                duration = media.wav_duration(part_wav)
                for word, start, end in captions._scene_words(chunk, part_mp3.with_suffix(".json"), duration):
                    spoken_words.append((word, offset + start, offset + end))
                offset += duration
        else:
            spoken_words = captions._scene_words(narration, mp3.with_suffix(".json"), speech_duration)
    if keep_mp3 and spoken_words and (kept.get("narration") != signature or not keep_mp3.is_file()):
        _keep_voice(audio, len(chunks), spoken, keep_mp3, keep_json, signature, spoken_words)
    intro = 3.0 if scenes and scenes[0].get("preview") else 0.0
    parts = []
    if intro:
        intro_wav = audio / "intro.wav"
        if not intro_wav.exists() or abs(media.wav_duration(intro_wav) - intro) > .02:
            media.silence(intro_wav, intro)
        parts.append((intro_wav, 0.0))
    parts.append((spoken, 0.0))
    media.concat_wavs(parts, cache / "narration.wav")
    words = [(word, intro + start, intro + end, 0) for word, start, end in spoken_words]
    total = intro + speech_duration
    total_text_words = sum(len(text.split()) for text in texts) or 1
    cumulative_text_words = 0
    cuts, previous = [0.0], 0.0
    for index, text in enumerate(texts):
        if index == 0 and intro and scenes[index].get("preview"):
            boundary = intro
        else:
            cumulative_text_words += len(text.split())
            word_index = round(cumulative_text_words / total_text_words * len(spoken_words))
            if word_index <= 0:
                boundary = previous
            elif word_index >= len(spoken_words):
                boundary = total
            else:
                boundary = intro + (spoken_words[word_index - 1][2] + spoken_words[word_index][1]) / 2
        boundary = min(max(boundary, previous), total)
        cuts.append(boundary)
        previous = boundary
    if cuts:
        cuts[-1] = total
    # A title card must remain readable; take a little time from the following scene without adding silence.
    for index, scene in enumerate(scenes[:-1]):
        if not scene.get("chapter"):
            continue
        current = cuts[index + 1] - cuts[index]
        available = cuts[index + 2] - cuts[index + 1]
        wanted = min(2.4, current + max(available - .8, 0))
        if current < wanted:
            cuts[index + 1] += wanted - current
    durations = [round(end - start, 3) for start, end in zip(cuts, cuts[1:])]
    return durations, words


def _keep_voice(audio: Path, chunk_count: int, spoken: Path, keep_mp3: Path, keep_json: Path, signature: str,
                words: list):
    """The ordered voice-over goes to the project's language folder: the API's own MP3 parts glued without
    re-encoding (or the WAV packed into MP3 when the parts are gone), plus the word timings for the subtitles."""
    try:
        keep_mp3.parent.mkdir(parents=True, exist_ok=True)
        tmp = keep_mp3.with_name(f".{keep_mp3.stem}.tmp.mp3")
        parts = [audio / f"narration-part-{n:03d}.mp3" for n in range(1, chunk_count + 1)]
        if parts and all(p.is_file() for p in parts):
            listing = audio / "keep.txt"
            listing.write_text("".join(f"file '{p.resolve()}'\n" for p in parts), "utf-8")
            media.run(["-f", "concat", "-safe", 0, "-i", listing.resolve(), "-c", "copy", tmp.resolve()], "Озвучка в папку проекта")
            listing.unlink(missing_ok=True)
        else:
            media.run(["-i", spoken.resolve(), "-c:a", "libmp3lame", "-b:a", "192k", tmp.resolve()], "Озвучка в папку проекта")
        tmp.replace(keep_mp3)
        # Timings measured on this very file, so the subtitles match it exactly when it is used again.
        _write_json(keep_json, {"narration": signature, "words": [[w, round(a, 3), round(b, 3)] for w, a, b in words]})
    except (OSError, UserError):
        pass  # keeping a copy is a convenience: the video is still built from the internal one


def _link_or_copy(source: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _project_id(path: str, lang: str) -> str:
    # English keeps the id of earlier versions, so a build in progress is not started over after the update.
    return hashlib.sha256(("folder:" + path + ("" if lang == "en" else f":{lang}")).encode()).hexdigest()[:12]


def _text_lang(text: str) -> str:
    low = text.lower()
    if re.search(r"[а-яё]", low):
        return "ru"
    if re.search(r"[äöüß]", low) or re.search(r"\b(der|die|das|und|nicht|ist)\b", low):
        return "de"
    if re.search(r"[ñ¿¡]", low) or re.search(r"\b(el|los|las|que|una|del)\b", low):
        return "es"
    return "en"


def _chapter_labels(scenes: list[dict], lang: str, settings: dict, cache: Path) -> dict[str, str]:
    """Chapter titles in the video's language: the scene file may be written in another one."""
    titles = {s["id"]: s["chapter"] for s in scenes if s.get("chapter")}
    if not titles:
        return {}
    pending = {k: v for k, v in titles.items() if _text_lang(v) != lang}
    if not pending or not llm.has_key(settings):
        return titles
    path = cache / "chapters.json"
    mark = hashlib.sha256(json.dumps([lang, pending], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    saved = _read_json(path, {})
    if saved.get("mark") == mark:
        return {**titles, **(saved.get("titles") or {})}
    from .pipeline import _helper_settings
    schema = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"}, "title": {"type": "string"}}, "required": ["id", "title"]}}}, "required": ["items"]}
    try:
        r = llm.call_json(_helper_settings(settings), "You translate short chapter titles of a documentary. Answer only with JSON.",
                          f"Translate each chapter title into {LANG_NAMES[lang]}. Keep it short, punchy and natural, the way a "
                          f"native YouTube documentary would title it; keep names and dates.\n\n"
                          + "\n".join(f"{k}: {v}" for k, v in pending.items()), schema, {}, 4000)
        done = {x["id"]: x["title"].strip() for x in r.get("items") or [] if x.get("id") in pending and x.get("title")}
    except UserError:
        return titles
    _write_json(path, {"mark": mark, "titles": done})
    return {**titles, **done}


def _package_chapters(root: Path, lang: str, scenes: list[dict], texts: list[str],
                      durations: list[float]) -> tuple[dict[str, str], list[float]]:
    """Chapters from the «0:00 Title» lines of загрузка_YouTube_<LANG>.md, for a scene file without chapter cards.
    Those times are estimates (words ÷ words per minute), so each one is turned back into a word of the narration,
    the chapter starts at the scene that holds that word, and its real time is measured on the voice-over.
    Returns ({scene id: title} for the chapter plates (not the opening one), [real start of every chapter])."""
    home = _lang_home(root, lang)
    file = upload_package.find_file(home, lang) if home else None
    if not file:
        return {}, []
    package = upload_package.parse(file)
    marks = package.get("chapters") or []
    if len(marks) < 2:
        return {}, []
    counts = [len((t or "").split()) for t in texts]
    starts_word, starts_time, total_w, total_t = [], [], 0, 0.0
    for count, duration in zip(counts, durations):
        starts_word.append(total_w)
        starts_time.append(total_t)
        total_w += count
        total_t += float(duration or 0)
    wpm = max(60, int(package.get("wpm") or 150))
    plates, times, prev = {}, [], -1
    for seconds, title in marks:
        if seconds <= 0:
            times.append(0.0)
            prev = max(prev, 0)
            continue
        word = min(total_w - 1, round(seconds / 60 * wpm))
        # The first scene that starts at or after the estimated word: chapters begin on a cut.
        index = next((i for i, w in enumerate(starts_word) if w >= word), len(scenes) - 1)
        index = max(index, prev + 1)
        if index >= len(scenes):
            break
        prev = index
        times.append(starts_time[index])
        plates[scenes[index]["id"]] = title
    if len(times) != len(marks):
        return plates, []
    return plates, times


MOTION_KINDS = ("map", "timeline", "chart", "compare")


def _motion_inserts(job_id: str, scenes: list[dict], texts: list[str], durations: list[float], media_map: dict[str, Path],
                    lang: str, settings: dict, options: dict, cache: Path, progress: float) -> dict[str, dict]:
    """A few maps, timelines and charts drawn in place of pictures, only in the first half of the video and only where
    the narration names places, dates or numbers. Returns {scene id: {"clip": Path, "spec": dict}}."""
    if not options.get("folder_motion_inserts", True) or not llm.has_key(settings) or len(scenes) < 6:
        return {}
    total = sum(durations) or 1
    half, elapsed, candidates = total / 2, 0.0, []
    for i, (scene, duration) in enumerate(zip(scenes, durations)):
        start, elapsed = elapsed, elapsed + duration
        source = media_map.get(scene["id"])
        if start >= half:
            break
        # Only pictures (the owner's videos stay), never the preview or a chapter card, and long enough to read.
        if i == 0 or scene.get("preview") or scene.get("chapter") or not source or source.suffix.lower() in VIDEO_EXT:
            continue
        if not 4.0 <= duration <= 18.0 or len(texts[i].split()) < 6:
            continue
        candidates.append(i)
    if not candidates:
        return {}
    limit = max(1, min(4, round(half / 150)))
    plan_path = cache / "motion.json"
    mark = hashlib.sha256(json.dumps([lang, limit, [texts[i] for i in candidates]], ensure_ascii=False).encode()).hexdigest()
    saved = _read_json(plan_path, {})
    if saved.get("mark") == mark:
        items = saved.get("items") or []
    else:
        _update(job_id, progress=progress, step=f"[{lang.upper()}] Ищу места для карт и линий времени в первой половине…")
        rows = [{"text": texts[i]} for i in candidates]
        prompt = motion.PLAN_PROMPT.format(limit=limit, language=LANG_NAMES[lang],
                                           scenes="\n".join(f"{k}. {r['text']}" for k, r in enumerate(rows)))
        prompt += ("\n\nOnly use the kinds map, timeline, chart and compare. These scenes are only some of the video, "
                   "so the rule about the first and the last scene does not apply; choose fewer rather than weak ones.")
        from .pipeline import _helper_settings
        try:
            r = llm.call_json(_helper_settings(settings), "You plan motion design scenes. Answer only with JSON.",
                              prompt, motion.SCHEMA, {}, 8000)
            raw = [g for g in r.get("items") or [] if isinstance(g, dict) and g.get("type") in MOTION_KINDS]
            # clean_plan drops the first and the last of the list: pad the list so every candidate can be chosen.
            for g in raw:
                try:
                    g["index"] = int(g.get("index")) + 1
                except (TypeError, ValueError):
                    g["index"] = -1
            items = [dict(g, index=candidates[g["index"] - 1]) for g in motion.clean_plan(raw, len(candidates) + 2)][:limit]
        except UserError:
            items = []
        _write_json(plan_path, {"mark": mark, "items": items})
    out = {}
    fps = int(settings.get("fps") or 30)
    for g in items:
        _check_control(job_id)
        i = int(g["index"])
        scene = scenes[i]
        clip = cache / "motion" / f"{scene['id']}_{hashlib.sha256(json.dumps([g, durations[i]], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:10]}.mp4"
        if not clip.exists():
            _update(job_id, step=f"[{lang.upper()}] Рисую {motion.KINDS[g['type']].split(' ', 1)[1].lower()} для сцены {scene['id']}…")
            prev = media_map.get(scenes[i - 1]["id"]) if i else None
            prev = prev if prev and prev.suffix.lower() in IMAGE_EXT else media_map.get(scene["id"])
            try:
                clip.parent.mkdir(parents=True, exist_ok=True)
                tmp = clip.with_suffix(".tmp.mp4")
                motion.render(g, tmp, durations[i], 1920, 1080, fps, lang, prev=prev)
                tmp.replace(clip)
            except Exception:  # noqa: BLE001 - a motion insert is an extra, never a reason to lose the video
                continue
        out[scene["id"]] = {"clip": clip, "spec": g}
    return out


MUSIC_BRIEF_NAMES = ("музыка.txt", "music.txt")


def _music_brief(root) -> dict | None:
    """The project's own music description: исходники/музыка.txt.
    Lines «Стиль: …» (2–4 Russian words for the library) and «Prompt: …» (English, for the generator);
    without those labels the whole text is the prompt. Lines starting with # are notes."""
    root = Path(root)
    if not root.is_dir():
        # The project folder was moved, renamed or deleted: no brief, and the queue page must still open.
        return None
    folders = [p for p in root.iterdir() if p.is_dir() and p.name.lower() == "исходники"] + [root]
    for folder in folders:
        for name in MUSIC_BRIEF_NAMES:
            f = folder / name
            if not f.is_file():
                continue
            style, prompt, free = "", "", []
            for line in f.read_text("utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                low = line.lower()
                if low.startswith(("стиль:", "style:")):
                    style = line.split(":", 1)[1].strip()
                elif low.startswith(("prompt:", "промпт:", "промт:")):
                    prompt = line.split(":", 1)[1].strip()
                else:
                    free.append(line)
            prompt = prompt or " ".join(free)
            if prompt:
                return {"prompt": prompt[:1500], "style_ru": style or "своя музыка проекта", "file": str(f)}
    return None


def _wants_compose(options: dict, root) -> bool:
    """Compose when the setting says so, or when the project carries its own music description."""
    if not options.get("folder_music", True):
        return False
    return options.get("folder_music_source") == "compose" or _music_brief(root) is not None


def _project_music(job_id: str, analysis: dict, scenes: list[dict], settings: dict, options: dict) -> Path | None:
    """One new track per project, composed once and shared by all its languages; it also lands in the music
    library, so it can be used again for free."""
    job = _jobs[job_id]
    saved = Path(str(job.get("music_track") or ""))
    if saved.is_file():
        return saved
    _update(job_id, step="Сочиняю музыку для проекта…")
    english = (analysis["languages"].get("en") or next(iter(analysis["languages"].values())))["narration_file"]
    opening = " ".join(Path(english).read_text("utf-8", errors="replace").split()[:220])
    prompt = ("Cinematic documentary underscore, atmospheric and restrained, slow evolving strings and soft piano, "
              "low pulse, no vocals, sits under a narrator")
    style = "документальная, атмосферная"
    brief = _music_brief(analysis["path"])
    if brief:
        # The scriptwriter's own description wins: the mood is chosen per channel, not guessed.
        prompt, style = brief["prompt"] + ". Instrumental, no vocals, sits quietly under a narrator", brief["style_ru"]
    elif llm.has_key(settings):
        from .pipeline import _helper_settings
        schema = {"type": "object", "properties": {"prompt": {"type": "string"}, "style_ru": {"type": "string"}},
                  "required": ["prompt", "style_ru"]}
        try:
            r = llm.call_json(_helper_settings(settings), "You are a music supervisor for narrated YouTube documentaries. Answer only with JSON.",
                              "Write a prompt for an AI music generator: an instrumental background bed for this video, "
                              "under a narrator for its whole length. Genre, mood, tempo, instruments; calm enough not to "
                              "fight the voice, no vocals, no big drops. Max 40 words, English. style_ru: 2-4 Russian words "
                              f"describing the style.\n\nTitle: {analysis['title']}\nOpening of the narration: {opening}",
                              schema, {}, 2000)
            prompt, style = r.get("prompt") or prompt, r.get("style_ru") or style
        except UserError:
            pass
    track = music.compose(settings, prompt, 120, style, analysis["title"])
    _update(job_id, music_track=str(track))
    return track


def _local_project(job_id: str, analysis: dict, scenes: list[dict], texts: list[str], media_map: dict[str, Path],
                   durations: list[float], words: list, settings: dict, options: dict, cache: Path,
                   lang: str = "en", inserts: dict | None = None, chapters: dict | None = None,
                   track: Path | None = None) -> tuple[str, Path]:
    inserts = inserts or {}
    pid = _project_id(analysis["path"], lang)
    project_dir = local_editor.ROOT / pid
    media_dir, render_dir = project_dir / "media", project_dir / "render"
    media_dir.mkdir(parents=True, exist_ok=True)
    render_dir.mkdir(parents=True, exist_ok=True)
    project = local_editor._default_project(pid, analysis["title"], "long")
    project.update({
        "fps": int(settings.get("fps") or 30), "music_volume": float(options.get("folder_music_volume", .12)),
        "edit_style": options.get("folder_edit_style") or "auto", "edit_tempo": options.get("folder_edit_tempo") or "balanced",
        "effect_level": options.get("folder_effect_level") or "normal", "render_speed": options.get("folder_render_speed") or "fast",
        "subtitles": bool(options.get("folder_subtitles", True)), "subtitles_position": options.get("folder_subtitles_position") or "mid_low",
        "subtitle_preset": options.get("folder_subtitle_preset") or "karaoke", "subtitle_font": options.get("folder_subtitle_font") or "auto",
        "subtitle_size": options.get("folder_subtitle_size") or "medium", "graphics_enabled": bool(options.get("folder_auto_graphics", True) or options.get("folder_chapters", True)),
        "graphics_auto": bool(options.get("folder_auto_graphics", True)), "auto_sfx": True,
        "render_parallel": max(1, min(int(options.get("render_parallel") or 3), 4)),
    })
    local_scenes = []
    for i, (scene, duration) in enumerate(zip(scenes, durations)):
        insert = inserts.get(scene["id"])
        source = insert["clip"] if insert else media_map[scene["id"]]
        ext = source.suffix.lower()
        local_name = hashlib.sha256((scene["id"] + str(source)).encode()).hexdigest()[:12] + ext
        _link_or_copy(source, media_dir / local_name)
        kind = "video" if ext in VIDEO_EXT else "image"
        source_duration = media.video_duration(source) if kind == "video" else duration
        focus_x, focus_y = local_editor._image_focus(source) if kind == "image" else (.5, .5)
        # A single still must never sit unchanged for 20–30 seconds: create virtual camera shots from it.
        parts = max(1, math.ceil(duration / 11.0)) if kind == "image" else 1
        used = 0.0
        for part in range(parts):
            part_duration = round(duration / parts, 3) if part + 1 < parts else round(duration - used, 3)
            used += part_duration
            local_scenes.append({
                "id": hashlib.sha256(f"{scene['id']}:{part}".encode()).hexdigest()[:12],
                "scene_code": scene["id"], "segment": part + 1, "segments": parts, "source": local_name,
                "name": source.name, "kind": kind, "source_duration": round(source_duration, 3), "trim_start": 0,
                "trim_end": round(min(source_duration, duration), 3), "focus_x": focus_x, "focus_y": focus_y,
                "duration": part_duration, "fit": "cover", "motion": "none", "transition": "cut",
                "effect": "none", "sfx": "none", "graphic": {"type": "none", "value": "", "unit": "", "label": "", "start": .2,
                "style": "classic", "animation": "pop", "auto": False},
                **({"motion_insert": insert["spec"].get("type")} if insert else {}),
            })
    project["scenes"] = local_scenes
    project["scenes_synced"] = True
    project["transcript"] = {"status": "done", "text": " ".join(texts), "words": [[w, s, e] for w, s, e, *_ in words],
                             "language": lang, "model": "ElevenLabs", "error": "", "step": "Готово"}
    project["language"] = lang
    narration = cache / "narration.wav"
    _link_or_copy(narration, media_dir / "narration.wav")
    project["voice"] = {"source": "narration.wav", "name": f"{LANG_NAMES[lang]} voice", "duration": round(sum(durations), 3)}
    recent = [str(item.get("style") or "") for item in local_editor._editing_history()
              if item.get("project_id") != pid]
    editing_styles.build_plan(project, recent)
    for target in project["scenes"]:
        if float(target.get("duration") or 0) < 1.0:
            target["transition"], target["sfx"] = "cut", "none"
        if target.get("motion_insert"):
            # A drawn map moves by itself: no camera move or effect on top of it.
            target["motion"], target["effect"] = "none", "none"
    if options.get("folder_music", True):
        plan = project.get("plan") or {}
        mood = " ".join([str(plan.get("style_name") or ""), str(plan.get("description") or ""),
                           analysis["title"], " ".join(texts[:5])])
        picked = track or music.pick(mood)
        if picked:
            local_music = hashlib.sha256(str(picked).encode()).hexdigest()[:12] + picked.suffix.lower()
            _link_or_copy(picked, media_dir / local_music)
            project["music"] = {"source": local_music, "name": picked.name, "duration": media.video_duration(picked)}
    project["subtitle_preset"] = options.get("folder_subtitle_preset") or "karaoke"
    if options.get("folder_auto_graphics", True):
        try:
            local_editor._apply_graphic_suggestions(project)
        except UserError:
            pass
        for target in project["scenes"]:
            if target.get("motion_insert") and (target.get("graphic") or {}).get("auto"):
                target["graphic"] = {"type": "none", "value": "", "unit": "", "label": "", "start": .2,
                                     "style": "classic", "animation": "pop", "auto": False}
    chapters = chapters or {}
    if options.get("folder_chapters", True):
        for source in scenes:
            if source.get("chapter") or source["id"] in chapters:
                target = next((item for item in project["scenes"] if item.get("scene_code") == source["id"]), None)
                if not target:
                    continue
                target["graphic"] = {"type": "chapter", "value": "", "unit": "", "label": chapters.get(source["id"]) or source["chapter"], "start": .15,
                                     "style": "cinematic", "animation": "pop", "auto": True}
    (project_dir / "edit.json").write_text(json.dumps(project, ensure_ascii=False, indent=2), "utf-8")
    return pid, project_dir


def _fast_language(job_id: str, analysis: dict, scenes: list[dict], texts: list[str], media_map: dict[str, Path],
                   durations: list[float], words: list, options: dict, cache: Path, lang: str, track: Path | None,
                   info: dict, lo: float, share: float, outputs: dict, done: dict):
    """«Быстрая сборка»: the pictures as they are, the voice, music and subtitles - one encoding pass."""
    music_path = None
    if options.get("folder_music", True):
        music_path = track or music.pick(" ".join([analysis["title"], " ".join(texts[:5])]))
    output = Path(info["output"])
    output.parent.mkdir(parents=True, exist_ok=True)
    work = cache / "fast"
    lo, hi = lo + share * .4, lo + share * .98

    def progress(fraction: float, text: str):
        with _lock:
            if (_jobs.get(job_id) or {}).get("status") in ("pausing", "stopping"):
                return
        _update(job_id, progress=round(lo + (hi - lo) * fraction, 3), step=f"[{lang.upper()}] Быстрая сборка: {text}")

    def cancelled() -> bool:
        with _lock:
            return (_jobs.get(job_id) or {}).get("status") in ("pausing", "stopping")

    try:
        fast_render.render(
            work, output.with_suffix(".new.mp4"), [media_map[scene["id"]] for scene in scenes], durations,
            cache / "narration.wav", words, music_path, float(options.get("folder_music_volume", .12)),
            bool(options.get("folder_subtitles", True)),
            {"preset": options.get("folder_subtitle_preset") or "karaoke", "font": options.get("folder_subtitle_font") or "auto",
             "size": options.get("folder_subtitle_size") or "medium", "position": options.get("folder_subtitles_position") or "mid_low"},
            fps=int(load_settings().get("fps") or 30), parallel=max(2, min(int(options.get("render_parallel") or 4), 6)),
            on_progress=progress, cancelled=cancelled)
    except fast_render.Cancelled:
        _check_control(job_id)
        raise _StopRequested()
    output.with_suffix(".new.mp4").replace(output)
    outputs[lang] = str(output)
    done[lang] = str(output)
    _update(job_id, langs_done=done, outputs=outputs, output=str(output))
    fast_render.clear(work)


def _drop_drafts(job: dict):
    """The montage drafts of one folder (all its languages): only a render cache, rebuilt for free if needed."""
    for lang in LANGS:
        shutil.rmtree(local_editor.ROOT / _project_id(str(job.get("path") or ""), lang), ignore_errors=True)


def _delete_generated_frames(analysis: dict):
    """After a finished build: the pictures the program drew (кадры/S###.png) go, the owner's own files stay.
    The thumbnail base stays too, so a thumbnail can still be redone without drawing it again."""
    frame_dir = Path(analysis["frame_dir"])
    manifest_path = frame_dir / ".autoyoutube-generated.json"
    manifest = _read_json(manifest_path, {})
    for sid in (manifest.get("frames") or {}) if isinstance(manifest, dict) else {}:
        (frame_dir / f"{sid}.png").unlink(missing_ok=True)
    manifest_path.unlink(missing_ok=True)
    shutil.rmtree(frame_dir / "референсы", ignore_errors=True)
    try:
        if frame_dir.is_dir() and not any(p for p in frame_dir.iterdir() if p.name != ".DS_Store"):
            shutil.rmtree(frame_dir, ignore_errors=True)
    except OSError:
        pass


def _clean_leftovers():
    """At start: montage drafts and caches of folders that are finished or gone from the queue. Drafts of folders
    that can still go on (queued, paused, stopped, waiting) stay, so their montage resumes where it stopped."""
    with _lock:
        jobs = list(_jobs.values())
    # Only finished videos are cleaned: a build that is waiting, paused, failed or cut off by an update keeps its
    # drafts, so its montage goes on where it stopped.
    keep = {local_editor.ROOT / _project_id(str(j.get("path") or ""), lang)
            for j in jobs if j.get("status") != "done" for lang in LANGS}
    if local_editor.ROOT.is_dir():
        for folder in local_editor.ROOT.iterdir():
            if not folder.is_dir() or folder in keep:
                continue
            edit = _read_json(folder / "edit.json", {})
            # Only drafts made by «Сборка из папок» (their scenes carry S### codes), never the owner's own montages.
            if any(isinstance(sc, dict) and sc.get("scene_code") for sc in edit.get("scenes") or []):
                shutil.rmtree(folder, ignore_errors=True)
    ids = {j.get("id") for j in jobs}
    for folder in ROOT.iterdir() if ROOT.is_dir() else []:
        if folder.is_dir() and folder.name not in ids:
            shutil.rmtree(folder, ignore_errors=True)


def _render_language(job_id: str, pid: str, project_dir: Path, lo: float, hi: float, lang: str) -> Path:
    with local_editor._lock:
        local_editor._rendering.add(pid)
    abort = threading.Event()

    def render_cancelled():
        if abort.is_set():
            return True
        with _lock:
            return (_jobs.get(job_id) or {}).get("status") in ("pausing", "stopping")

    render_thread = threading.Thread(target=local_editor._render_worker, args=(pid, "final", render_cancelled),
                                     daemon=True, name=f"folder-final-{job_id}-{lang}")
    render_thread.start()
    try:
        while render_thread.is_alive():
            render_state = _read_json(project_dir / "edit.json", {}).get("render", {})
            render_progress = float(render_state.get("progress") or 0)
            if not render_cancelled():
                _update(job_id, progress=round(lo + (hi - lo) * render_progress, 3),
                        step=f"[{lang.upper()}] " + (render_state.get("step") or "Собираю финальное видео 1080p…"))
            render_thread.join(.6)
    finally:
        if render_thread.is_alive():
            # Anything went wrong here: the montage must not go on alone in the background (it would fill the
            # disk and run next to the next project's montage); it stops at its next step and is waited for.
            abort.set()
            render_thread.join()
    _check_control(job_id)
    rendered = project_dir / "render" / "final.mp4"
    result = _read_json(project_dir / "edit.json", {}).get("render", {})
    if result.get("status") != "done" or not rendered.is_file():
        raise UserError(result.get("error") or "Финальная сборка не завершилась.")
    return rendered


# Montage gate: rendering (and motion inserts) loads the CPU, so only one project renders at a time.
# Projects behind it keep drawing pictures, composing music and ordering voice-overs meanwhile.
_render_cond = threading.Condition()
_render_owner: str | None = None
_render_waiting: list[str] = []


def _render_gate_enter(job_id: str):
    global _render_owner
    with _render_cond:
        if _render_owner == job_id:
            return
        if job_id not in _render_waiting:
            _render_waiting.append(job_id)
    while True:
        with _render_cond:
            # Fair order: the project that was added to the queue first goes first.
            order = sorted(_render_waiting, key=lambda j: (_jobs.get(j) or {}).get("added", 0))
            if _render_owner is None and order and order[0] == job_id:
                _render_waiting.remove(job_id)
                _render_owner = job_id
                return
            owner = (_jobs.get(_render_owner) or {}).get("title") if _render_owner else None
        if owner:
            _update(job_id, step=f"Озвучка и картинки готовы. Жду очереди на монтаж (сейчас монтируется «{owner}»)…")
        try:
            _check_control(job_id)
        except (_PauseRequested, _StopRequested):
            with _render_cond:
                if job_id in _render_waiting:
                    _render_waiting.remove(job_id)
            raise
        with _render_cond:
            _render_cond.wait(1.0)


def _render_gate_leave(job_id: str):
    global _render_owner
    with _render_cond:
        if job_id in _render_waiting:
            _render_waiting.remove(job_id)
        if _render_owner == job_id:
            _render_owner = None
        _render_cond.notify_all()


def _build(job_id: str):
    try:
        _check_control(job_id)
        job = _jobs[job_id]
        analysis = _analyze(Path(job["path"]))
        settings = load_settings()
        options = {**settings, **(job.get("options") or {})}
        fast = bool(options.get("folder_fast"))
        if fast and scene_binding(options) == "smart":
            options["folder_scene_binding"] = "hints"  # the fast build never waits for a model call
        langs = [x for x in selected_langs(options) if x in analysis["languages"]]
        if not langs:
            raise UserError("В папке нет текста озвучки на выбранных языках.")
        cache = ROOT / job_id
        cache.mkdir(parents=True, exist_ok=True)
        scenes, refs = _parse_scenes(Path(analysis["scene_file"]))
        built = dict(job.get("langs_done") or {})
        all_built = all(built.get(x) and Path(built[x]).is_file() for x in langs)
        media_map = {}
        if not all_built:
            _update(job_id, progress=.03, step="Проверяю готовые кадры и создаю недостающие…")
            media_map = _make_images(job_id, analysis, scenes, refs, settings, options)
        _check_control(job_id)
        track = None
        if not all_built and not fast and _wants_compose(options, analysis["path"]):
            try:
                track = _project_music(job_id, analysis, scenes, settings, options)
            except UserError as exc:
                _update(job_id, warnings=list(job.get("warnings") or []) + [f"Музыку сочинить не получилось ({exc}), беру из библиотеки."])
        english_file = (analysis["languages"].get("en") or {}).get("narration_file")
        english = Path(english_file).read_text("utf-8", errors="replace").strip() if english_file else ""
        done = dict(job.get("langs_done") or {})
        outputs, failures = {}, []
        # A language already published keeps counting as done even when its MP4 went to the Trash afterwards.
        published = (_jobs.get(job_id) or {}).get("uploaded") or {}
        todo = [x for x in langs if not (done.get(x) and (Path(done[x]).is_file() or x in published))]
        for lang in langs:
            if lang not in todo:
                outputs[lang] = done[lang]
        share = .52 / max(len(todo), 1)
        project_dirs = []
        # Stage 1 (network, runs in parallel with other projects): text mapping, voice-over, chapter titles.
        prepared = {}
        for n, lang in enumerate(todo):
            lo = .48 + share * n
            info = analysis["languages"][lang]
            lang_cache = cache if lang == "en" else cache / lang
            lang_cache.mkdir(parents=True, exist_ok=True)
            try:
                narration = Path(info["narration_file"]).read_text("utf-8", errors="replace").strip()
                texts = _mapping(job_id, scenes, narration, options, lang_cache, english if lang != "en" else "", lo)
                _check_control(job_id)
                own = _scan_media(Path(analysis["path"]), lang)
                lang_media = {**media_map, **{sid: path for sid, path in own.items()
                                              if any(part.lower() == lang for part in path.parent.parts[-3:])}}
                _update(job_id, progress=round(lo + share * .05, 3), step=f"[{lang.upper()}] Готовлю озвучку…")
                durations, words = _make_voice(job_id, scenes, texts, narration, settings, options, lang_cache, lang,
                                               (lo + share * .05, lo + share * .35), Path(info["output"]).parent)
                _check_control(job_id)
                chapters = _chapter_labels(scenes, lang, settings, lang_cache) if options.get("folder_chapters", True) and not fast else {}
                if not any(s.get("chapter") for s in scenes):
                    # The plain scene file has no chapter cards: chapters come from the upload file's timecodes.
                    plates, times = _package_chapters(Path(analysis["path"]), lang, scenes, texts, durations)
                    if options.get("folder_chapters", True) and not fast:
                        chapters = plates
                    if times:
                        with _lock:
                            _jobs[job_id].setdefault("chapter_times", {})[lang] = times
                        _save_queue()
                prepared[lang] = (lo, info, lang_cache, texts, lang_media, durations, words, chapters)
            except (_PauseRequested, _StopRequested):
                raise
            except Exception as exc:  # one language failing must not lose the others
                failures.append(f"{lang.upper()}: {exc}")
        # Stage 2 (CPU): motion inserts and montage, one project at a time.
        if prepared:
            _render_gate_enter(job_id)
        try:
            for lang, (lo, info, lang_cache, texts, lang_media, durations, words, chapters) in prepared.items():
                try:
                    _check_control(job_id)
                    _wait_for_space(job_id, _montage_need(sum(durations)),
                                    {local_editor.ROOT / _project_id(analysis["path"], lang)})
                    if fast:
                        _fast_language(job_id, analysis, scenes, texts, lang_media, durations, words, options,
                                       lang_cache, lang, track, info, lo, share, outputs, done)
                        continue
                    inserts = _motion_inserts(job_id, scenes, texts, durations, lang_media, lang, settings, options,
                                              lang_cache, round(lo + share * .36, 3))
                    _check_control(job_id)
                    _update(job_id, progress=round(lo + share * .4, 3), step=f"[{lang.upper()}] Распределяю движения, переходы, главы и моушен…")
                    pid, project_dir = _local_project(job_id, analysis, scenes, texts, lang_media, durations, words, settings,
                                                      options, lang_cache, lang, inserts, chapters, track)
                    project_dirs.append(project_dir)
                    _check_control(job_id)
                    rendered = _render_language(job_id, pid, project_dir, lo + share * .42, lo + share * .98, lang)
                    output = Path(info["output"])
                    output.parent.mkdir(parents=True, exist_ok=True)
                    tmp = output.with_suffix(".new.mp4")
                    _link_or_copy(rendered, tmp)
                    tmp.replace(output)
                    outputs[lang] = str(output)
                    done[lang] = str(output)
                    _update(job_id, langs_done=done, outputs=outputs, output=str(output))
                    shutil.rmtree(project_dir, ignore_errors=True)
                except (_PauseRequested, _StopRequested):
                    raise
                except Exception as exc:  # one language failing must not lose the others
                    failures.append(f"{lang.upper()}: {exc}")
        finally:
            _render_gate_leave(job_id)
        if failures:
            raise UserError("Не собрались: " + "; ".join(failures)
                            + (f". Готовы: {', '.join(x.upper() for x in outputs)}" if outputs else ""))
        upload_now = bool(options.get("folder_upload") or (_jobs.get(job_id) or {}).get("force_upload"))
        if options.get("folder_thumbs", True) or upload_now:
            _make_thumbs(job_id, analysis, outputs, settings)
        if upload_now:
            _publish(job_id, analysis, outputs, settings, options)
        first = outputs.get("en") or next(iter(outputs.values()))
        _update(job_id, status="done", progress=1, step="Готово", error="", output=first, outputs=outputs, force_upload=False,
                finished=_now(), analysis=_analyze(Path(job["path"])))
        # The source folder keeps its generated scene PNGs and final MP4s; internal audio, clips and duplicate finals do not.
        shutil.rmtree(cache, ignore_errors=True)
        for project_dir in project_dirs:
            shutil.rmtree(project_dir, ignore_errors=True)
        _drop_drafts(job)
        shutil.rmtree(Path(analysis["frame_dir"]) / "референсы", ignore_errors=True)
        # The pictures stay in the project folder (кадры): a rebuild never draws and pays for them again.
    except _ReviewRequested as review:
        need = review.args[0] if review.args else []
        _update(job_id, status="review", progress=.99, error="",
                step=("Ролики собраны. Выберите, на какой канал выложить ("
                      + ", ".join(x.upper() for x in need) + "), и нажмите «Выложить»." if need else
                      "Ролики собраны. Проверьте превью и нажмите «Выложить»."))
    except _PauseRequested:
        _update(job_id, status="paused", step="На паузе", error="")
    except _StopRequested:
        _update(job_id, status="stopped", step="Остановлено", error="")
    except Exception as exc:
        tries = int((_jobs.get(job_id) or {}).get("auto_retries") or 0)
        if "No space left" in str(exc) or getattr(exc, "errno", None) == 28:
            # A full disk is not the project's fault: it waits in line without using up its attempts.
            at = _now() + 5 * 60
            _update(job_id, status="queued", retry_at=at, error="",
                    last_error="На диске кончилось место. Освободите место — продолжу сам.",
                    step=f"На диске кончилось место. Освободите место — продолжу сам в {time.strftime('%H:%M', time.localtime(at))}.")
            _kick_scheduler()
        elif tries < JOB_AUTO_RETRIES and not _fatal(exc):
            # Night queue: the project goes back in line and is tried again later; finished stages are not redone.
            at = _now() + JOB_RETRY_WAIT
            _update(job_id, status="queued", auto_retries=tries + 1, retry_at=at, last_error=str(exc)[:400], error="",
                    step=f"Ошибка, повторю сам в {time.strftime('%H:%M', time.localtime(at))} (попытка {tries + 2} из {JOB_AUTO_RETRIES + 1})")
            _kick_scheduler()
        else:
            _update(job_id, status="error", step="Ошибка", error=str(exc), retry_at=0)


def _make_thumbs(job_id: str, analysis: dict, outputs: dict, settings: dict):
    """A thumbnail next to each finished video (<LANG>/превью_<LANG>.png), from section «2. Превью» of the upload
    file. A thumbnail that does not work out is reported, the videos stay done."""
    job = _jobs[job_id]
    root = Path(analysis["path"])
    en_dir = _lang_home(root, "en")
    en_file = upload_package.find_file(en_dir, "en") if en_dir else None
    english = upload_package.parse(en_file) if en_file else None
    thumbs = dict(job.get("thumbs") or {})
    generated = dict(job.get("thumbs_generated") or {})
    problems = {}
    for lang in outputs:
        folder = _lang_home(root, lang)
        if not folder:
            continue
        own = upload_package.finished_thumb(folder, lang)
        if own and generated.get(lang) != str(own):
            thumbs[lang] = str(own)  # the owner's own thumbnail: used as it is
            continue
        file = upload_package.find_file(folder, lang)
        if not file:
            problems[lang] = f"нет файла загрузка_YouTube_{lang.upper()}.md: не знаю, что написать на превью"
            continue
        package = upload_package.parse(file)
        _check_control(job_id)
        _update(job_id, progress=.99, step=f"[{lang.upper()}] Рисую превью…")
        try:
            thumb = upload_package.make_thumb(root, lang, folder, package, settings, english)
        except UserError as exc:
            problems[lang] = str(exc)
            continue
        generated[lang] = str(thumb)
        thumbs[lang] = str(thumb)
    _update(job_id, thumbs=thumbs, thumbs_generated=generated, thumb_problems=problems)


def _to_trash(path: Path) -> str:
    """Moves a published MP4 to the Mac's Trash (it can be put back from there). Returns "" if it could not."""
    if not path.is_file():
        return ""
    if sys.platform == "darwin":
        # Through Finder: the file gets «Put Back» in the Trash. macOS may ask once to let the program use Finder.
        script = f'tell application "Finder" to delete (POSIX file {json.dumps(str(path.resolve()))} as alias)'
        try:
            r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=60)
            if r.returncode == 0 and not path.exists():
                return "Корзина"
        except (OSError, subprocess.TimeoutExpired):
            pass
    trash = Path.home() / ".Trash"
    if trash.is_dir():
        target = trash / path.name
        if target.exists():
            target = trash / f"{path.stem} {time.strftime('%H-%M-%S')}{path.suffix}"
        try:
            shutil.move(str(path), str(target))
            return str(target)
        except OSError:
            pass
    return ""


# Scheduled publishing: the viewers' time zone of each language (English = the USA, Spanish = Spain).
SCHEDULE_ZONES = {"en": ("America/New_York", "Нью-Йорку"), "es": ("Europe/Madrid", "Мадриду"),
                  "de": ("Europe/Berlin", "Берлину"), "ru": ("Europe/Moscow", "Москве")}
_slot_lock = threading.Lock()


def _slot_time(text: str) -> tuple[int, int]:
    m = re.match(r"\s*(\d{1,2})[:.](\d{2})", str(text or ""))
    h, mi = (int(m.group(1)), int(m.group(2))) if m else (15, 0)
    return (h if 0 <= h < 24 else 15), (mi - mi % 15 if 0 <= mi < 60 else 0)  # Studio offers times every 15 minutes


def _reserve_slot(key: str, lang: str, at: str):
    """One video a day per YouTube channel: the first day from today whose «at» (viewers' time) is still at least
    two hours away and has no video of this channel yet. The day is written down at once, so two videos built side
    by side never get the same day; _release_slot gives it back if the upload fails."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    zone = ZoneInfo(SCHEDULE_ZONES.get(lang, ("UTC", ""))[0])
    hour, minute = _slot_time(at)
    now = datetime.now(zone)
    with _slot_lock:
        slots = dict(load_settings().get("upload_slots") or {})
        taken = set(slots.get(key) or [])
        day = now.date()
        while True:
            when = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
            if when - now >= timedelta(hours=2) and day.isoformat() not in taken:
                break
            day += timedelta(days=1)
        keep = sorted(d for d in taken if d >= (now.date() - timedelta(days=3)).isoformat())
        slots[key] = keep + [day.isoformat()]
        save_settings({"upload_slots": slots})
    return when


def _release_slot(key: str, when):
    with _slot_lock:
        slots = dict(load_settings().get("upload_slots") or {})
        slots[key] = [d for d in slots.get(key) or [] if d != when.date().isoformat()]
        save_settings({"upload_slots": slots})


def _delete_mp4(path: Path, forever: bool) -> str:
    """After a checked upload: the MP4 is deleted for good (the Mac is short of space) or goes to the Trash."""
    if not forever:
        return _to_trash(path)
    try:
        path.unlink()
        return "удалён"
    except OSError:
        return ""


def _profile_for(job: dict, channel: str, lang: str, rules: dict) -> str:
    """The profile picked for this video, else the channel's; ASK when the channel wants a pick that is not made."""
    return (job.get("octo_pick") or {}).get(lang) or rules.get(f"{channel}|{lang}") or ""


def _publish(job_id: str, analysis: dict, outputs: dict, settings: dict, options: dict):
    """Thumbnails, the owner's check if asked for, then each language goes to its channel's Octo profile."""
    from . import studio_upload
    job = _jobs[job_id]
    root = Path(analysis["path"])
    channel = root.parent.name
    rules = settings.get("octo_rules") or {}
    uploaded = dict(job.get("uploaded") or {})
    approved = job.get("approved") or {}
    generated = dict(job.get("thumbs_generated") or {})
    en_dir = _lang_home(root, "en")
    en_file = upload_package.find_file(en_dir, "en") if en_dir else None
    english = upload_package.parse(en_file) if en_file else None
    ready, skipped, thumbs = {}, {}, dict(job.get("thumbs") or {})
    for lang, out in outputs.items():
        if lang in uploaded:
            continue
        folder = _lang_home(root, lang)
        file = upload_package.find_file(folder, lang) if folder else None
        if not file:
            skipped[lang] = f"нет файла загрузки_YouTube_{lang.upper()}.md"
            continue
        if not _profile_for(job, channel, lang, rules):
            skipped[lang] = f"не выбран профиль Octo для «{channel} · {lang.upper()}»"
            continue
        package = upload_package.parse(file)
        real = (job.get("chapter_times") or {}).get(lang)
        if real:
            # The description's timecodes were estimated before the voice-over existed: put the measured ones.
            package["description"] = upload_package.retime_description(package["description"], real)
        if not package.get("title"):
            skipped[lang] = "в файле загрузки нет названия (раздел «1. Название»)"
            continue
        _check_control(job_id)
        own = upload_package.finished_thumb(folder, lang)
        if not own or generated.get(lang) == str(own):
            _update(job_id, progress=.99, step=f"[{lang.upper()}] Готовлю превью…")
        thumb = upload_package.make_thumb(root, lang, folder, package, settings, english)
        if not own:
            generated[lang] = str(thumb)
        thumbs[lang] = str(thumb)
        ready[lang] = (package, thumb)
    _update(job_id, thumbs=thumbs, thumbs_generated=generated, upload_skipped=skipped)
    need_pick = [lang for lang in ready if _profile_for(job, channel, lang, rules) == ASK]
    if need_pick or (options.get("folder_thumb_review", True) and any(not approved.get(lang) for lang in ready)):
        raise _ReviewRequested(need_pick)
    failures = []
    privacy = options.get("folder_upload_privacy") or "public"
    for lang, (package, thumb) in ready.items():
        _check_control(job_id)
        uuid = _profile_for(job, channel, lang, rules)
        tag = f"[{lang.upper()}]"
        try:
            with _profile_lock(uuid):
                _update(job_id, progress=.99, step=f"{tag} Запускаю профиль Octo…")
                ws = octo.start(uuid)
                check: dict = {}
                yt_key = f"{channel}|{lang}|{uuid}"
                yt = (load_settings().get("octo_yt_channels") or {}).get(yt_key) or {}
                when = None
                if privacy == "public" and options.get("folder_schedule", True):
                    when = _reserve_slot(yt_key, lang, options.get("folder_schedule_time") or "15:00")
                    city = SCHEDULE_ZONES.get(lang, ("", "UTC"))[1]
                    _update(job_id, step=f"{tag} Выйдет {when.strftime('%d.%m')} в {when.strftime('%H:%M')} по {city}")
                try:
                    link = studio_upload.upload(
                        ws, Path(outputs[lang]), package, thumb, privacy,
                        log=lambda m, t=tag: _update(job_id, step=f"{t} {m}"),
                        cancelled=lambda: (_jobs.get(job_id) or {}).get("status") in ("pausing", "stopping"),
                        result=check, channel_ref=yt.get("id") or yt.get("ref") or "", schedule=when)
                except BaseException:
                    if when is not None:
                        _release_slot(yt_key, when)
                    raise
                finally:
                    # The program closes the profile it used, so profiles do not pile up open; the owner can mark
                    # some profiles «не закрывать» (settings octo_keep_open). A screenshot of a failure is already saved.
                    if uuid not in (load_settings().get("octo_keep_open") or []):
                        _update(job_id, step=f"{tag} Закрываю профиль Octo…")
                        octo.stop(uuid)
            uploaded[lang] = link or "опубликовано"
            _update(job_id, uploaded=uploaded)
            if when is not None:
                planned = dict(job.get("scheduled") or {})
                planned[lang] = (f"{when.strftime('%d.%m %H:%M')} по {SCHEDULE_ZONES.get(lang, ('', 'UTC'))[1]}"
                                 if check.get("scheduled") else "дата не встала — ролик скрыт, поставьте дату в Studio")
                _update(job_id, scheduled=planned)
                if not check.get("scheduled"):
                    failures.append(f"{lang.upper()}: ролик загружен, но дату публикации Studio не принял — он сохранён "
                                    f"скрытым; поставьте в Studio «Запланировать» на {when.strftime('%d.%m %H:%M')} "
                                    f"по {SCHEDULE_ZONES.get(lang, ('', 'UTC'))[1]}")
            if check.get("channel_id"):
                # The channel it went to is remembered for this profile: next time Studio is opened right on it.
                all_yt = dict(load_settings().get("octo_yt_channels") or {})
                all_yt[yt_key] = {**yt, "id": check["channel_id"], "name": check.get("channel_name") or yt.get("name", "")}
                save_settings({"octo_yt_channels": all_yt})
            if check.get("thumb_ok") is False:
                failures.append(f"{lang.upper()}: ролик опубликован, но ПРЕВЬЮ НЕ ПОСТАВИЛОСЬ — поставьте его в Studio руками")
            if options.get("folder_trash_mp4", True) and check.get("verified"):
                forever = load_settings().get("folder_mp4_forever", True)
                where = _delete_mp4(Path(outputs[lang]), forever)
                trashed = dict(job.get("trashed") or {})
                trashed[lang] = where or ""
                done = "MP4 удалён" if forever else "MP4 отправлен в Корзину"
                _update(job_id, trashed=trashed,
                        step=f"{tag} " + (done if where else "MP4 удалить не удалось — лежит на месте"))
        except UserError as exc:
            failures.append(f"{lang.upper()}: {exc}")
    _check_control(job_id)
    if failures:
        raise UserError("Не выложились: " + "; ".join(failures)
                        + (f". Выложены: {', '.join(x.upper() for x in uploaded)}" if uploaded else ""))


def _schedule():
    global _scheduler
    while True:
        settings = load_settings()
        limit = max(1, min(int(settings.get("folder_jobs_parallel") or 2), 3))
        with _lock:
            running = sum(job.get("status") in ("running", "pausing", "stopping") for job in _jobs.values())
            waiting = [job for job in _jobs.values() if job.get("status") == "queued"]
            # A project that failed waits for its retry time; the others go first.
            queued = [job for job in waiting if (job.get("retry_at") or 0) <= time.time()]
            while queued and running < limit:
                job = queued.pop(0)
                job.update(status="running", step="Проверяю проект…", progress=.01, started=_now(), error="")
                thread = threading.Thread(target=_build, args=(job["id"],), daemon=True, name=f"folder-build-{job['id']}")
                job["_thread"] = thread
                thread.start()
                running += 1
            _save_queue()
            if not waiting and running == 0:
                _scheduler = None
                return
        time.sleep(.8)


def _kick_scheduler():
    global _scheduler
    with _lock:
        if _scheduler and _scheduler.is_alive():
            return
        _scheduler = threading.Thread(target=_schedule, daemon=True, name="folder-build-queue")
        _scheduler.start()


# ---------------------------------------------------------------- автоподхват из «Папки входящих»

_inbox_wake = threading.Event()


def _inbox_public() -> dict:
    state = folder_inbox.load_state(INBOX_STATE_FILE)
    out = {k: state.get(k) for k in ("last_check", "picked", "errors", "last_error", "last_picked", "status",
                                      "github_last", "github_downloaded", "github_status")}
    out["github_token_set"] = bool(load_settings().get("github_token"))
    return out


_github_next = 0.0


def _github_fetch(settings: dict, inbox: Path, state: dict, force: bool = False):
    """Новые архивы из закрытого репозитория сценариев → в папку входящих (дальше их берёт scan_once)."""
    global _github_next
    repo = folder_inbox.normalize_repo(str(settings.get("folder_github_repo") or ""))
    if not repo:
        state["github_status"] = "Неверно указан репозиторий: нужно «владелец/название»." if settings.get("folder_github_repo") else ""
        return
    if not force and time.time() < _github_next:
        return
    _github_next = time.time() + GITHUB_INTERVAL
    from urllib.parse import quote
    branch = str(settings.get("folder_github_branch") or "").strip() or "main"
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = str(settings.get("github_token") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    api = f"https://api.github.com/repos/{repo}"
    state["github_last"] = _now()
    try:
        tree = net.request("GitHub", "GET", f"{api}/git/trees/{quote(branch, safe='')}", tries=2, timeout=30,
                           headers=headers, params={"recursive": "1"}).json().get("tree") or []
    except UserError as exc:
        text = str(exc)
        if "repository is empty" in text.lower():
            state["github_status"] = "репозиторий пока пустой — доступ есть, ждём первый сценарий."
        elif "409" in text or "not found" in text.lower() and token:
            state["github_status"] = f"нет ветки «{branch}» или репозиторий пустой. ({text[:160]})"
        else:
            hint = "" if token else " Если репозиторий закрытый — вставьте токен GitHub."
            state["github_status"] = f"{text}{hint}"
        return

    def download(path: str) -> bytes:
        return net.request("GitHub", "GET", f"{api}/contents/{quote(path)}", tries=3, timeout=300,
                           headers={**headers, "Accept": "application/vnd.github.raw"}, params={"ref": branch}).content

    state["github_status"] = ""
    folder_inbox.github_pull(tree, inbox, state, download, log=lambda m: print(m, flush=True))


def _inbox_is_project(path: Path) -> bool:
    try:
        _analyze(path)
        return True
    except Exception:
        return False


def _inbox_enqueue(path: Path, channel: str):
    added = _add_paths([str(path)])
    settings = load_settings()
    if settings.get("folder_inbox_autostart") and added:
        _queue_jobs(set(added), settings, True)
        _kick_scheduler()


def inbox_pass(force_github: bool = False) -> dict:
    """One pass of the watcher with the saved settings (also used by «Проверить сейчас»)."""
    settings = load_settings()
    inbox, work = str(settings.get("folder_inbox_dir") or "").strip(), str(settings.get("folder_work_dir") or "").strip()
    if not inbox or not work:
        return _inbox_public()
    state = folder_inbox.load_state(INBOX_STATE_FILE)
    try:
        if Path(inbox).expanduser().is_dir():
            _github_fetch(settings, Path(inbox).expanduser(), state, force_github)
    except Exception as exc:
        state["github_status"] = f"Ошибка: {exc}"
    try:
        def work_for(channel: str) -> Path:
            path, owner = channel_work_dir(settings, channel)
            if owner and _work_dir_problem(path, inbox):
                return Path(work).expanduser()  # a broken channel folder must not send its archives to «_ошибки»
            return Path(path).expanduser()

        folder_inbox.scan_once(Path(inbox), Path(work), state, is_project=_inbox_is_project, enqueue=_inbox_enqueue,
                               work_for=work_for, log=lambda m: print(m, flush=True))
    except Exception as exc:  # the watcher must survive anything
        state["status"] = f"Ошибка проверки: {exc}"
    try:
        _write_json(INBOX_STATE_FILE, state)
    except OSError:
        pass
    return _inbox_public()


def _inbox_loop():
    time.sleep(5)
    force = True
    while True:
        try:
            inbox_pass(force)
        except Exception:
            pass
        force = _inbox_wake.wait(INBOX_INTERVAL)  # «Проверить сейчас» also asks GitHub right away
        _inbox_wake.clear()


@router.post("/inbox/check")
def inbox_check_now():
    _inbox_wake.set()
    return {"ok": True, "inbox": _inbox_public()}


_load_queue()
threading.Thread(target=_inbox_loop, daemon=True, name="folder-inbox").start()
