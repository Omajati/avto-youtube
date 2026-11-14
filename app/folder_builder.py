"""One-click assembly of externally prepared project folders.

The source folder remains the source of truth.  Existing S001... media are read in
place, missing pictures are generated into ``кадры``, and only the finished MP4 is
written to the language folder.  Audio and render intermediates stay in app data so
the owner's project folder does not fill up with service files.
"""
from __future__ import annotations

import hashlib
import json
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

from . import captions, editing_styles, images, llm, local_editor, media, music, tts
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
_lock = threading.RLock()
_jobs: dict[str, dict] = {}
_scheduler: threading.Thread | None = None


class _PauseRequested(Exception):
    pass


class _StopRequested(Exception):
    pass


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
            changed = True
    if changed:
        _save_queue()


def _save_queue():
    with _lock:
        public = {key: {k: v for k, v in job.items() if k != "_thread"} for key, job in _jobs.items()}
        _write_json(QUEUE_FILE, public)


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


def _parse_scenes(path: Path) -> tuple[list[dict], list[dict]]:
    text = path.read_text("utf-8", errors="replace")
    matches = list(SCENE_HEAD.finditer(text))
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
    for path in root.rglob("*.md"):
        if any(part.startswith(".") or part.lower() in ("готово", "output", "render") for part in path.relative_to(root).parts[:-1]):
            continue
        try:
            count = len(SCENE_HEAD.findall(path.read_text("utf-8", errors="ignore")))
        except OSError:
            continue
        if count:
            candidates.append((count, path))
    if not candidates:
        raise UserError("Не найден Markdown-файл с описаниями сцен S001, S002…")
    return max(candidates, key=lambda item: item[0])[1]


def _find_narration(root: Path) -> Path:
    en = root / "EN"
    candidates = list(en.glob("*озвучка*EN*.txt")) + list(en.glob("*voice*EN*.txt")) if en.is_dir() else []
    if not candidates and en.is_dir():
        candidates = [p for p in en.glob("*.txt") if "озвуч" in p.name.lower() or "voice" in p.name.lower()]
    if not candidates:
        candidates = [p for p in root.rglob("*.txt") if "озвуч" in p.name.lower() and "de" not in p.name.lower()]
    if not candidates:
        raise UserError("Не найден окончательный английский текст озвучки в папке EN.")
    return min(candidates, key=lambda p: (len(p.parts), len(p.name)))


def _scan_media(root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXT | VIDEO_EXT:
            continue
        rel = path.relative_to(root)
        if any(part.startswith((".", "_")) or part.lower() in ("готово", "output", "render") for part in rel.parts[:-1]):
            continue
        match = SCENE_FILE.match(path.name)
        if not match:
            continue
        sid = f"S{int(match.group(1)):03d}"
        old = found.get(sid)
        if old is None or (path.suffix.lower() in VIDEO_EXT and old.suffix.lower() not in VIDEO_EXT):
            found[sid] = path
    return found


def _analyze(path: Path) -> dict:
    path = path.expanduser().resolve()
    if not path.is_dir():
        raise UserError(f"Папка не найдена: {path}")
    scene_file = _find_scene_file(path)
    narration_file = _find_narration(path)
    scenes, refs = _parse_scenes(scene_file)
    existing = _scan_media(path)
    videos = sum(existing.get(s["id"], Path("x")).suffix.lower() in VIDEO_EXT for s in scenes if s["id"] in existing)
    pictures = sum(existing.get(s["id"], Path("x")).suffix.lower() in IMAGE_EXT for s in scenes if s["id"] in existing)
    missing = [s["id"] for s in scenes if s["id"] not in existing]
    text = narration_file.read_text("utf-8", errors="replace").strip()
    frame_dir = scene_file.parent / "кадры"
    output = path / "EN" / f"{path.name}_EN.mp4"
    return {
        "path": str(path), "title": path.name, "scene_file": str(scene_file), "narration_file": str(narration_file),
        "frame_dir": str(frame_dir), "output": str(output), "scenes": len(scenes), "references": len(refs),
        "existing_videos": videos, "existing_images": pictures, "missing_images": len(missing),
        "missing_ids": missing[:30], "words": len(text.split()),
        "styled_scenes": sum(bool(re.search(r"\bstyle\s*:", scene.get("prompt") or "", re.I)) for scene in scenes),
    }


def _setting_summary(settings: dict) -> dict:
    provider = images.PROVIDERS.get(settings.get("image_provider"), images.PROVIDERS["fal"])
    image_model = settings.get(provider["model_field"]) or provider["models"][0][0]
    llm_model = settings.get("openai_model") if settings.get("llm_provider") == "openai" else settings.get("anthropic_model")
    return {
        "image_provider": provider["name"], "image_model": image_model,
        "llm_provider": "OpenAI" if settings.get("llm_provider") == "openai" else "Claude",
        "llm_model": llm_model, "voice_id": settings.get("folder_voice_en_id") or settings.get("elevenlabs_voice_id"),
        "voice_name": settings.get("folder_voice_en_name") or settings.get("elevenlabs_voice_name") or "",
        "music_tracks": len(music.tracks()),
    }


def _problems(settings: dict, analysis: dict) -> tuple[list[str], list[str]]:
    problems, warnings = [], []
    provider = images.PROVIDERS.get(settings.get("image_provider"), images.PROVIDERS["fal"])
    if analysis.get("missing_images") and not settings.get(provider["key_field"]):
        problems.append(f"Не указан ключ для {provider['name']}.")
    if not llm.has_key(settings):
        problems.append("Не указан ключ выбранной языковой модели для распределения текста по сценам.")
    if settings.get("voice_provider") == "royal":
        if not settings.get("royal_key"):
            problems.append("Не указан ключ RoyalTechno для озвучки.")
    elif not settings.get("elevenlabs_key"):
        problems.append("Не указан ключ ElevenLabs для озвучки.")
    if not (settings.get("folder_voice_en_id") or settings.get("elevenlabs_voice_id")):
        problems.append("Не выбран английский голос.")
    if settings.get("folder_references", True) and analysis.get("references") and not images.can_reference(settings):
        warnings.append("Референсы включены, но выбранный сервис не умеет рисовать по образцу: обычные сцены будут созданы по тексту.")
    if settings.get("folder_music", True) and not music.tracks():
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
def list_queue():
    settings = load_settings()
    with _lock:
        jobs = [_public_job(job) for job in sorted(_jobs.values(), key=lambda x: x.get("added", 0))]
    for job in jobs:
        if job.get("analysis"):
            job["problems"], job["warnings"] = _problems(settings, job["analysis"])
    return {"jobs": jobs, "settings": {k: settings.get(k) for k in (
        "folder_voice_en_id", "folder_voice_en_name", "folder_smart_mapping", "folder_references", "folder_chapters", "folder_subtitles",
        "folder_subtitle_preset", "folder_subtitle_font", "folder_subtitle_size", "folder_subtitles_position",
        "folder_music", "folder_music_volume", "folder_auto_graphics", "folder_edit_style", "folder_edit_tempo",
        "folder_effect_level", "folder_render_speed", "folder_jobs_parallel", "image_parallel", "render_parallel")},
        "summary": _setting_summary(settings)}


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


@router.post("/add")
def add_folders(data: dict = Body(...)):
    added = []
    for raw in data.get("paths") or []:
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
    return {"added": added}


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
    return {"ok": True}


@router.post("/start")
def start_queue(data: dict = Body(...)):
    options = data.get("options") or {}
    allowed = {k for k in (
        "folder_voice_en_id", "folder_voice_en_name", "folder_smart_mapping", "folder_references", "folder_chapters", "folder_subtitles",
        "folder_subtitle_preset", "folder_subtitle_font", "folder_subtitle_size", "folder_subtitles_position",
        "folder_music", "folder_music_volume", "folder_auto_graphics", "folder_edit_style", "folder_edit_tempo",
        "folder_effect_level", "folder_render_speed", "folder_jobs_parallel", "image_parallel", "render_parallel")}
    save_settings({k: v for k, v in options.items() if k in allowed})
    settings = load_settings()
    ids = set(data.get("ids") or _jobs)
    queued = []
    with _lock:
        for jid, job in _jobs.items():
            if jid not in ids or job.get("status") in ("running", "pausing", "stopping"):
                continue
            analysis = _analyze(Path(job["path"]))
            problems, warnings = _problems(settings, analysis)
            job["analysis"], job["warnings"] = analysis, warnings
            if problems:
                job.update(status="error", error=" ".join(problems), step="", progress=0)
                continue
            job.update(status="queued", error="", step="Ждёт своей очереди…", progress=0, options={k: settings.get(k) for k in allowed})
            queued.append(jid)
        _save_queue()
    _kick_scheduler()
    return {"queued": queued}


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
        elif action == "resume":
            if status not in ("paused", "stopped", "error"):
                raise UserError("Этот проект сейчас нельзя продолжить.")
            job.update(status="queued", step="Ждёт своей очереди…", error="")
        else:
            raise UserError("Неизвестная команда управления проектом.")
        job["updated"] = _now()
        _save_queue()
    if action in ("stop", "resume"):
        _kick_scheduler()
    return {"ok": True, "status": job.get("status")}


@router.post("/{job_id}/reveal")
def reveal(job_id: str):
    job = _jobs.get(job_id)
    path = Path(str((job or {}).get("output") or ""))
    if not path.is_file():
        raise UserError("Готовый ролик ещё не найден.")
    subprocess.Popen(["open", "-R", str(path)] if sys.platform == "darwin" else ["xdg-open", str(path.parent)])
    return {"ok": True}


def _sentences(text: str) -> list[str]:
    items = [m.group(0).strip() for m in re.finditer(r".+?(?:[.!?…]+[\"”’»)]*(?=\s+|$)|$)", text.strip(), re.S)]
    return [x for x in items if x]


def _fast_mapping(scenes: list[dict], narration: str) -> tuple[list[str], int]:
    """Map scene starts to the exact `Голос:` fragments locally; no model or transcription call."""
    raw_words = narration.split()
    norm_words, owners = [], []
    for raw_index, word in enumerate(raw_words):
        for token in re.findall(r"[a-z0-9]+", word.lower().replace("’", "'")):
            norm_words.append(token)
            owners.append(raw_index)

    def segments(hint: str) -> list[list[str]]:
        hint = re.sub(r"(?i)\bauthor\s*:\s*", "", hint)
        chunks = re.split(r"(?:…+|\.{2,})", hint)
        result = []
        for chunk in chunks:
            tokens = re.findall(r"[a-z0-9]+", chunk.lower().replace("’", "'"))
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
        take = min(5, max(1, (following - opening) // 3)) if following > opening else 0
        starts[index] = opening
        starts[index + 1] = min(following, opening + take)
    for index in range(1, len(starts)):
        starts[index] = max(starts[index], starts[index - 1])

    ends = starts[1:] + [len(raw_words)]
    texts = [" ".join(raw_words[start:end]).strip() for start, end in zip(starts, ends)]
    return texts, matched


def _mapping(job_id: str, scenes: list[dict], narration: str, settings: dict, cache: Path) -> list[str]:
    smart = bool(settings.get("folder_smart_mapping", False))
    sentences = _sentences(narration)
    fingerprint = hashlib.sha256((narration + json.dumps(scenes, ensure_ascii=False, sort_keys=True) + str(smart)).encode()).hexdigest()
    saved = _read_json(cache / "mapping.json", {})
    if saved.get("fingerprint") == fingerprint and len(saved.get("texts") or []) == len(scenes):
        return saved["texts"]
    if not smart:
        _update(job_id, progress=.04, step="Быстро привязываю сцены к подсказкам «Голос:»…")
        texts, matched = _fast_mapping(scenes, narration)
        _write_json(cache / "mapping.json", {"fingerprint": fingerprint, "texts": texts, "mode": "fast", "matched": matched})
        return texts
    _update(job_id, progress=.04, step="OpenAI уточняет распределение текста по сценам…")
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
        _update(job_id, step="OpenAI исчерпал лимит ответа — использую быстрые подсказки «Голос:»…")
        _write_json(cache / "mapping.json", {"fingerprint": fingerprint, "texts": texts, "mode": "fast-fallback", "matched": matched})
        return texts
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
    _write_json(cache / "mapping.json", {"fingerprint": fingerprint, "texts": texts, "usage": usage})
    return texts


def _reference_keywords(ref: dict) -> list[str]:
    name = re.sub(r"[^a-z0-9 ]", " ", ref.get("name", "").lower())
    return [x for x in name.split() if len(x) >= 4 and x not in {"reference", "today"}]


def _make_images(job_id: str, analysis: dict, scenes: list[dict], refs: list[dict], settings: dict, options: dict) -> dict[str, Path]:
    _check_control(job_id)
    root = Path(analysis["path"])
    frame_dir = Path(analysis["frame_dir"])
    frame_dir.mkdir(parents=True, exist_ok=True)
    media_map = _scan_media(root)
    use_refs = bool(options.get("folder_references", True))
    ref_paths: dict[str, Path] = {}
    if use_refs and refs:
        ref_dir = frame_dir / "референсы"
        ref_dir.mkdir(parents=True, exist_ok=True)
        for index, ref in enumerate(refs):
            _check_control(job_id)
            out = ref_dir / f"{ref['id']}.png"
            if not out.exists():
                _update(job_id, progress=.06 + .04 * (index / max(len(refs), 1)), step=f"Создаю референс {ref['id']}…")
                prompt = ref.get("prompt") or ref.get("name")
                media.to_png(images.generate(settings, prompt, False), out)
                _check_control(job_id)
            ref_paths[ref["id"]] = out
    missing = [scene for scene in scenes if scene["id"] not in media_map]
    if not missing:
        return media_map
    done = [0]

    def work(scene: dict):
        _check_control(job_id)
        out = frame_dir / f"{scene['id']}.png"
        if out.exists():
            return scene["id"], out
        prompt = (scene.get("prompt") or scene.get("voice_hint") or scene["id"]).strip()
        if "style:" not in prompt.lower() and settings.get("image_style"):
            prompt += ". " + str(settings["image_style"]).strip()
        selected = []
        low = prompt.lower()
        if use_refs and images.can_reference(settings):
            for ref in refs:
                keys = _reference_keywords(ref)
                if keys and (" ".join(keys) in low or any(k in low for k in keys)) and ref["id"] in ref_paths:
                    selected.append(ref_paths[ref["id"]])
        raw = images.generate_with_reference(settings, prompt, selected[:3], False) if selected else images.generate(settings, prompt, False)
        media.to_png(raw, out)
        _check_control(job_id)
        return scene["id"], out

    workers = max(1, min(int(options.get("image_parallel") or settings.get("image_parallel") or 4), 8))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"folder-images-{job_id}") as pool:
        futures = [pool.submit(work, scene) for scene in missing]
        try:
            for future in as_completed(futures):
                sid, out = future.result()
                media_map[sid] = out
                done[0] += 1
                _update(job_id, progress=.10 + .38 * done[0] / len(missing),
                        step=f"Создаю недостающие изображения: {done[0]} из {len(missing)}…")
                _check_control(job_id)
        except (_PauseRequested, _StopRequested):
            for future in futures:
                future.cancel()
            raise
    return media_map


def _make_voice(job_id: str, scenes: list[dict], texts: list[str], narration: str,
                settings: dict, options: dict, cache: Path):
    """Voice the complete narration with exactly one TTS request, then fit scenes to its word timings."""
    _check_control(job_id)
    audio = cache / "audio"
    audio.mkdir(parents=True, exist_ok=True)
    voice_settings = dict(settings)
    voice_settings["elevenlabs_voice_id"] = options.get("folder_voice_en_id") or settings.get("folder_voice_en_id") or settings.get("elevenlabs_voice_id")
    voice_settings["elevenlabs_voice_name"] = options.get("folder_voice_en_name") or settings.get("folder_voice_en_name") or settings.get("elevenlabs_voice_name")
    manifest_path = audio / "manifest.json"
    manifest = _read_json(manifest_path, {})
    mp3, spoken = audio / "narration.mp3", audio / "narration.wav"
    signature = hashlib.sha256(json.dumps([
        narration, voice_settings.get("elevenlabs_voice_id"), voice_settings.get("elevenlabs_model"),
        voice_settings.get("voice_speed"), voice_settings.get("voice_stability"), voice_settings.get("voice_style"),
    ], ensure_ascii=False).encode()).hexdigest()
    if manifest.get("narration") != signature or not spoken.exists():
        _update(job_id, progress=.49, step="Озвучиваю весь файл одним запросом…")
        tts.synthesize(voice_settings, narration, mp3, "", "", "en")
        media.to_wav(mp3, spoken)
        manifest = {"narration": signature}
        _write_json(manifest_path, manifest)
        _check_control(job_id)
    speech_duration = media.wav_duration(spoken)
    spoken_words = captions._scene_words(narration, mp3.with_suffix(".json"), speech_duration)
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
    durations = [round(end - start, 3) for start, end in zip(cuts, cuts[1:])]
    return durations, words


def _link_or_copy(source: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _local_project(job_id: str, analysis: dict, scenes: list[dict], texts: list[str], media_map: dict[str, Path],
                   durations: list[float], words: list, settings: dict, options: dict, cache: Path) -> tuple[str, Path]:
    pid = hashlib.sha256(("folder:" + analysis["path"]).encode()).hexdigest()[:12]
    project_dir = local_editor.ROOT / pid
    media_dir, render_dir = project_dir / "media", project_dir / "render"
    media_dir.mkdir(parents=True, exist_ok=True)
    render_dir.mkdir(parents=True, exist_ok=True)
    project = local_editor._default_project(pid, analysis["title"], "long")
    project.update({
        "fps": int(settings.get("fps") or 30), "music_volume": float(options.get("folder_music_volume") or .12),
        "edit_style": options.get("folder_edit_style") or "auto", "edit_tempo": options.get("folder_edit_tempo") or "balanced",
        "effect_level": options.get("folder_effect_level") or "normal", "render_speed": options.get("folder_render_speed") or "fast",
        "subtitles": bool(options.get("folder_subtitles", True)), "subtitles_position": options.get("folder_subtitles_position") or "mid_low",
        "subtitle_preset": options.get("folder_subtitle_preset") or "karaoke", "subtitle_font": options.get("folder_subtitle_font") or "auto",
        "subtitle_size": options.get("folder_subtitle_size") or "medium", "graphics_enabled": bool(options.get("folder_auto_graphics", True) or options.get("folder_chapters", True)),
        "graphics_auto": bool(options.get("folder_auto_graphics", True)), "auto_sfx": True,
    })
    local_scenes = []
    for i, (scene, duration) in enumerate(zip(scenes, durations)):
        source = media_map[scene["id"]]
        ext = source.suffix.lower()
        local_name = hashlib.sha256((scene["id"] + str(source)).encode()).hexdigest()[:12] + ext
        _link_or_copy(source, media_dir / local_name)
        kind = "video" if ext in VIDEO_EXT else "image"
        source_duration = media.video_duration(source) if kind == "video" else duration
        focus_x, focus_y = local_editor._image_focus(source) if kind == "image" else (.5, .5)
        local_scenes.append({
            "id": hashlib.sha256(scene["id"].encode()).hexdigest()[:12], "scene_code": scene["id"], "source": local_name,
            "name": source.name, "kind": kind, "source_duration": round(source_duration, 3), "trim_start": 0,
            "trim_end": round(min(source_duration, duration), 3), "focus_x": focus_x, "focus_y": focus_y,
            "duration": round(duration, 3), "fit": "cover", "motion": "none", "transition": "cut",
            "effect": "none", "sfx": "none", "graphic": {"type": "none", "value": "", "unit": "", "label": "", "start": .2,
            "style": "classic", "animation": "pop", "auto": False},
        })
    project["scenes"] = local_scenes
    project["scenes_synced"] = True
    project["transcript"] = {"status": "done", "text": " ".join(texts), "words": [[w, s, e] for w, s, e, *_ in words],
                             "language": "en", "model": "ElevenLabs", "error": "", "step": "Готово"}
    narration = cache / "narration.wav"
    _link_or_copy(narration, media_dir / "narration.wav")
    project["voice"] = {"source": "narration.wav", "name": "English voice", "duration": round(sum(durations), 3)}
    if options.get("folder_music", True):
        picked = music.pick(analysis["title"] + " " + " ".join(texts[:5]))
        if picked:
            local_music = hashlib.sha256(str(picked).encode()).hexdigest()[:12] + picked.suffix.lower()
            _link_or_copy(picked, media_dir / local_music)
            project["music"] = {"source": local_music, "name": picked.name, "duration": media.video_duration(picked)}
    editing_styles.build_plan(project, [])
    project["subtitle_preset"] = options.get("folder_subtitle_preset") or "karaoke"
    if options.get("folder_auto_graphics", True):
        try:
            local_editor._apply_graphic_suggestions(project)
        except UserError:
            pass
    if options.get("folder_chapters", True):
        for source, target in zip(scenes, project["scenes"]):
            if source.get("chapter"):
                target["graphic"] = {"type": "chapter", "value": "", "unit": "", "label": source["chapter"], "start": .15,
                                     "style": "cinematic", "animation": "pop", "auto": True}
    (project_dir / "edit.json").write_text(json.dumps(project, ensure_ascii=False, indent=2), "utf-8")
    return pid, project_dir


def _build(job_id: str):
    try:
        _check_control(job_id)
        job = _jobs[job_id]
        analysis = _analyze(Path(job["path"]))
        settings = load_settings()
        options = {**settings, **(job.get("options") or {})}
        cache = ROOT / job_id
        cache.mkdir(parents=True, exist_ok=True)
        scenes, refs = _parse_scenes(Path(analysis["scene_file"]))
        narration = Path(analysis["narration_file"]).read_text("utf-8", errors="replace").strip()
        texts = _mapping(job_id, scenes, narration, settings, cache)
        _check_control(job_id)
        _update(job_id, progress=.05, step="Проверяю готовые кадры и создаю недостающие…")
        media_map = _make_images(job_id, analysis, scenes, refs, settings, options)
        _check_control(job_id)
        _update(job_id, progress=.49, step="Готовлю единую английскую озвучку…")
        durations, words = _make_voice(job_id, scenes, texts, narration, settings, options, cache)
        _check_control(job_id)
        _update(job_id, progress=.72, step="Распределяю движения, переходы, главы и моушен…")
        pid, project_dir = _local_project(job_id, analysis, scenes, texts, media_map, durations, words, settings, options, cache)
        _check_control(job_id)
        _update(job_id, progress=.75, step="Собираю финальное видео 1080p…")
        with local_editor._lock:
            local_editor._rendering.add(pid)
        local_editor._render_worker(pid, "final")
        _check_control(job_id)
        rendered = project_dir / "render" / "final.mp4"
        result = _read_json(project_dir / "edit.json", {}).get("render", {})
        if result.get("status") != "done" or not rendered.is_file():
            raise UserError(result.get("error") or "Финальная сборка не завершилась.")
        output = Path(analysis["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        tmp = output.with_suffix(".new.mp4")
        shutil.copy2(rendered, tmp)
        tmp.replace(output)
        _update(job_id, status="done", progress=1, step="Готово", error="", output=str(output), finished=_now(), analysis=_analyze(Path(job["path"])))
    except _PauseRequested:
        _update(job_id, status="paused", step="На паузе", error="")
    except _StopRequested:
        _update(job_id, status="stopped", step="Остановлено", error="")
    except Exception as exc:
        _update(job_id, status="error", progress=0, step="", error=str(exc))


def _schedule():
    global _scheduler
    while True:
        settings = load_settings()
        limit = max(1, min(int(settings.get("folder_jobs_parallel") or 2), 3))
        with _lock:
            running = sum(job.get("status") in ("running", "pausing", "stopping") for job in _jobs.values())
            queued = [job for job in _jobs.values() if job.get("status") == "queued"]
            while queued and running < limit:
                job = queued.pop(0)
                job.update(status="running", step="Проверяю проект…", progress=.01, started=_now(), error="")
                thread = threading.Thread(target=_build, args=(job["id"],), daemon=True, name=f"folder-build-{job['id']}")
                job["_thread"] = thread
                thread.start()
                running += 1
            _save_queue()
            if not queued and running == 0:
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


_load_queue()
