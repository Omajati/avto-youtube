"""A small, fully local, non-destructive video editor.

Projects live next to the rest of the application data.  Original uploads are never
changed: the renderer only reads them and writes preview/final files into ``render``.
No network service or API key is used by this module.
"""
from __future__ import annotations

import array
import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse

from . import captions, graphics, local_whisper, media
from .config import DATA_DIR
from .net import UserError


router = APIRouter(prefix="/api/local-editor", tags=["local-editor"])
ROOT = DATA_DIR / "local_editor"
TRASH = ROOT / ".trash"
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".avif", ".heic"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus", ".webm", ".mp4", ".mov"}
PROJECT_ID = re.compile(r"[0-9a-f]{12}")
FILE_ID = re.compile(r"[0-9a-f]{12}\.[a-z0-9]{2,6}")
_lock = threading.RLock()
_rendering: set[str] = set()
_transcribing: set[str] = set()
LOCAL_GRAPHICS = {"none", "counter", "money", "percent", "bar", "timer", "stopwatch", "up", "down", "date", "rank", "keyword", "warning", "people", "location", "idea", "trophy", "name", "quote", "chapter", "document", "secret", "news", "timeline"}
GRAPHIC_STYLES = {"classic", "glass", "neon", "minimal", "cinematic", "broadcast"}
GRAPHIC_ANIMATIONS = {"pop", "slide", "pulse", "fade"}


def _now() -> int:
    return int(time.time())


def _project_dir(project_id: str) -> Path:
    if not PROJECT_ID.fullmatch(project_id or ""):
        raise HTTPException(404)
    path = ROOT / project_id
    if not path.is_dir():
        raise HTTPException(404)
    return path


def _read(project_id: str) -> dict:
    path = _project_dir(project_id) / "edit.json"
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise UserError(f"Не удалось открыть проект монтажа: {exc}") from exc


def _write(project: dict) -> dict:
    with _lock:
        path = _project_dir(project["id"]) / "edit.json"
        project["updated"] = _now()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(project, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(path)
    return project


def _duration(path: Path) -> float:
    return max(media.video_duration(path), 0.05)


def _public(project: dict) -> dict:
    out = deepcopy(project)
    pid = project["id"]
    for scene in out.get("scenes", []):
        scene["url"] = f"/api/local-editor/{pid}/media/{scene['source']}"
    for kind in ("voice", "music"):
        if out.get(kind):
            out[f"{kind}_url"] = f"/api/local-editor/{pid}/media/{out[kind]['source']}"
    render = out.setdefault("render", {})
    for quality in ("preview", "final"):
        name = f"{quality}.mp4"
        if (_project_dir(pid) / "render" / name).is_file():
            render[f"{quality}_url"] = f"/api/local-editor/{pid}/output/{quality}"
    if (_project_dir(pid) / "render" / "subtitles.srt").is_file():
        out.setdefault("transcript", {})["srt_url"] = f"/api/local-editor/{pid}/output/subtitles"
    out["duration"] = round(sum(float(s.get("duration") or 0) for s in out.get("scenes", [])), 3)
    return out


def _default_project(project_id: str, title: str, format_: str) -> dict:
    return {
        "id": project_id,
        "title": title.strip() or "Новый монтаж",
        "format": "short" if format_ == "short" else "long",
        "fps": 30,
        "music_volume": 0.12,
        "graphics_enabled": False,
        "graphics_auto": False,
        "subtitles": True,
        "subtitles_position": "mid_low",
        "scenes_synced": False,
        "voice": None,
        "music": None,
        "scenes": [],
        "transcript": {"status": "idle", "text": "", "words": [], "language": "", "model": "small", "error": "", "step": ""},
        "render": {"status": "idle", "progress": 0, "quality": "", "error": ""},
        "created": _now(),
        "updated": _now(),
    }


@router.get("")
def list_projects():
    ROOT.mkdir(parents=True, exist_ok=True)
    projects = []
    for path in ROOT.iterdir():
        if not path.is_dir() or not PROJECT_ID.fullmatch(path.name):
            continue
        try:
            projects.append(_public(json.loads((path / "edit.json").read_text("utf-8"))))
        except (OSError, ValueError):
            continue
    projects.sort(key=lambda p: p.get("updated", 0), reverse=True)
    return projects


@router.post("")
def create_project(data: dict = Body(default={})):
    ROOT.mkdir(parents=True, exist_ok=True)
    project_id = uuid.uuid4().hex[:12]
    path = ROOT / project_id
    (path / "media").mkdir(parents=True)
    (path / "render").mkdir()
    project = _default_project(project_id, str(data.get("title") or ""), str(data.get("format") or "long"))
    (path / "edit.json").write_text(json.dumps(project, ensure_ascii=False, indent=2), "utf-8")
    return _public(project)


@router.get("/{project_id}")
def get_project(project_id: str):
    project = _read(project_id)
    if project.get("render", {}).get("status") == "running" and project_id not in _rendering:
        project["render"].update(status="error", error="Сборка была прервана. Запустите её ещё раз.")
        _write(project)
    if project.get("transcript", {}).get("status") == "running" and project_id not in _transcribing:
        project["transcript"].update(status="error", error="Расшифровка была прервана. Запустите её ещё раз.")
        _write(project)
    return _public(project)


def _number(value, default: float, low: float, high: float) -> float:
    try:
        return min(max(float(value), low), high)
    except (TypeError, ValueError):
        return default


@router.put("/{project_id}")
def update_project(project_id: str, data: dict = Body(...)):
    project = _read(project_id)
    project["title"] = str(data.get("title", project["title"]))[:200].strip() or "Новый монтаж"
    project["format"] = "short" if data.get("format") == "short" else "long"
    project["fps"] = int(_number(data.get("fps", project.get("fps", 30)), 30, 24, 60))
    project["music_volume"] = _number(data.get("music_volume", project.get("music_volume", .12)), .12, 0, 1)
    project["graphics_enabled"] = bool(data.get("graphics_enabled", project.get("graphics_enabled", False)))
    project["graphics_auto"] = bool(data.get("graphics_auto", project.get("graphics_auto", False)))
    project["subtitles"] = bool(data.get("subtitles", project.get("subtitles", True)))
    project["subtitles_position"] = data.get("subtitles_position") if data.get("subtitles_position") in captions.POSITIONS else "mid_low"
    current = {s["id"]: s for s in project.get("scenes", [])}
    if isinstance(data.get("scenes"), list):
        scenes = []
        for sent in data["scenes"]:
            old = current.get(str(sent.get("id") or ""))
            if not old:
                continue
            scene = deepcopy(old)
            scene["fit"] = sent.get("fit") if sent.get("fit") in ("cover", "contain") else "cover"
            scene["motion"] = sent.get("motion") if sent.get("motion") in ("none", "zoom_in", "zoom_out", "pan_left", "pan_right") else "none"
            scene["transition"] = sent.get("transition") if sent.get("transition") in ("cut", "fade") else "cut"
            raw_graphic = sent.get("graphic") if isinstance(sent.get("graphic"), dict) else {}
            graphic_type = str(raw_graphic.get("type") or "none")
            if graphic_type not in LOCAL_GRAPHICS:
                graphic_type = "none"
            scene["graphic"] = {
                "type": graphic_type,
                "value": str(raw_graphic.get("value") or "")[:30],
                "unit": str(raw_graphic.get("unit") or "")[:12],
                "label": str(raw_graphic.get("label") or "")[:60],
                "start": round(_number(raw_graphic.get("start"), .2, 0, 3600), 2),
                "style": raw_graphic.get("style") if raw_graphic.get("style") in GRAPHIC_STYLES else "classic",
                "animation": raw_graphic.get("animation") if raw_graphic.get("animation") in GRAPHIC_ANIMATIONS else "pop",
                "auto": bool(raw_graphic.get("auto", False)),
            }
            scene["duration"] = _number(sent.get("duration"), float(old.get("duration") or 5), .1, 3600)
            if scene["kind"] == "video":
                source_duration = float(scene.get("source_duration") or scene["duration"])
                scene["trim_start"] = _number(sent.get("trim_start"), float(old.get("trim_start") or 0), 0, max(source_duration - .1, 0))
                scene["trim_end"] = _number(sent.get("trim_end"), float(old.get("trim_end") or source_duration), scene["trim_start"] + .1, source_duration)
            scenes.append(scene)
        project["scenes"] = scenes
    project["render"] = {"status": "idle", "progress": 0, "quality": "", "error": ""}
    return _public(_write(project))


@router.delete("/{project_id}")
def delete_project(project_id: str):
    path = _project_dir(project_id)
    if project_id in _rendering or project_id in _transcribing:
        raise UserError("Сначала дождитесь окончания текущей обработки.")
    TRASH.mkdir(parents=True, exist_ok=True)
    target = TRASH / f"{project_id}-{_now()}"
    shutil.move(str(path), str(target))
    return {"ok": True, "recoverable": True}


def _upload_ext(name: str, kind: str) -> tuple[str, str]:
    ext = Path(name).suffix.lower()
    if kind == "scene":
        if ext in IMAGE_EXT:
            return ext, "image"
        if ext in VIDEO_EXT:
            return ext, "video"
        raise UserError("Для таймлайна подходят картинки JPG, PNG, WEBP и видео MP4, MOV, WEBM, MKV.")
    if kind in ("voice", "music") and ext in AUDIO_EXT:
        return ext, "audio"
    raise UserError("Для озвучки и музыки подходят MP3, WAV, M4A, AAC, OGG, FLAC, OPUS или звук из видео.")


def _media_identity(name: str) -> tuple[str, str]:
    stem = Path(name).stem.lower().replace("ё", "е")
    stem = re.sub(
        r"(?:[\s_.-]*(?:video|vid|clip|animated|animation|motion|image|img|photo|"
        r"видео|клип|анимация|оживленное|оживленная|оживленный|фото|картинка))+$",
        "", stem,
    )
    core = re.sub(r"[^a-zа-я0-9]+", "", stem)
    numbers = re.findall(r"\d+", Path(name).stem)
    number = str(int(numbers[-1])) if numbers else ""
    return core, number


def _matching_scene(scenes: list[dict], name: str, kind: str) -> dict | None:
    candidates = [scene for scene in scenes if scene.get("kind") == kind]
    core, number = _media_identity(name)
    exact = [scene for scene in candidates if core and _media_identity(str(scene.get("name") or ""))[0] == core]
    if len(exact) == 1:
        return exact[0]
    numbered = [scene for scene in candidates if number and _media_identity(str(scene.get("name") or ""))[1] == number]
    return numbered[0] if len(numbered) == 1 else None


def _source_snapshot(scene: dict) -> dict:
    return {key: scene.get(key) for key in ("source", "name", "kind", "source_duration", "trim_start", "trim_end")}


def _activate_source(scene: dict, source: dict):
    duration = float(scene.get("duration") or source.get("source_duration") or 5)
    scene.update({key: source.get(key) for key in ("source", "name", "kind", "source_duration", "trim_start", "trim_end")})
    scene["duration"] = duration


@router.post("/{project_id}/upload")
async def upload(project_id: str, request: Request, name: str, kind: str = "scene"):
    project = _read(project_id)
    ext, media_kind = _upload_ext(Path(name).name, kind)
    raw = await request.body()
    if not raw:
        raise UserError("Файл пустой.")
    if len(raw) > 2 * 1024 * 1024 * 1024:
        raise UserError("Файл больше 2 ГБ.")
    filename = uuid.uuid4().hex[:12] + ext
    path = _project_dir(project_id) / "media" / filename
    path.write_bytes(raw)
    try:
        if kind == "scene":
            source_duration = _duration(path) if media_kind == "video" else 5.0
            source = {
                "source": filename, "name": Path(name).name, "kind": media_kind,
                "source_duration": round(source_duration, 3), "trim_start": 0,
                "trim_end": round(source_duration, 3),
            }
            opposite = "image" if media_kind == "video" else "video"
            matched = _matching_scene(project.get("scenes", []), Path(name).name, opposite)
            replaced = False
            if matched and media_kind == "video":
                matched["alternate_source"] = _source_snapshot(matched)
                _activate_source(matched, source)
                replaced = True
            elif matched:  # A photo uploaded after its video is kept as the optional still source.
                matched["alternate_source"] = source
                replaced = True
            else:
                scene = {
                    "id": uuid.uuid4().hex[:12], "source": filename, "name": Path(name).name,
                    "kind": media_kind, "source_duration": round(source_duration, 3),
                    "trim_start": 0, "trim_end": round(source_duration, 3),
                    "duration": round(source_duration, 3), "fit": "cover", "motion": "none", "transition": "cut",
                    "graphic": {"type": "none", "value": "", "unit": "", "label": "", "start": .2,
                                "style": "classic", "animation": "pop", "auto": False},
                }
                project.setdefault("scenes", []).append(scene)
                project["scenes_synced"] = False
        else:
            duration = _duration(path)
            old = project.get(kind)
            project[kind] = {"source": filename, "name": Path(name).name, "duration": round(duration, 3)}
            if kind == "voice":
                project["transcript"] = {"status": "idle", "text": "", "words": [], "language": "", "model": "small", "error": "", "step": ""}
                project["scenes_synced"] = False
                (path.parent.parent / "render" / "subtitles.srt").unlink(missing_ok=True)
            if old and FILE_ID.fullmatch(str(old.get("source") or "")):
                # Replaced files are kept in the media folder until the project is removed. This makes undo/recovery possible.
                pass
        project["render"] = {"status": "idle", "progress": 0, "quality": "", "error": ""}
        out = _public(_write(project))
        if kind == "scene":
            out["media_replaced"] = replaced
        return out
    except Exception:
        path.unlink(missing_ok=True)
        raise


@router.post("/{project_id}/match-media")
def match_media(project_id: str):
    project = _read(project_id)
    scenes = project.get("scenes", [])
    replaced = 0
    for video in [scene for scene in scenes if scene.get("kind") == "video"]:
        image = _matching_scene([scene for scene in scenes if scene is not video], str(video.get("name") or ""), "image")
        if not image:
            continue
        image["alternate_source"] = _source_snapshot(image)
        _activate_source(image, _source_snapshot(video))
        scenes.remove(video)
        replaced += 1
    if not replaced:
        raise UserError("Совпадений не найдено. Добавьте одинаковый номер сцены в имена фото и видео, например 012.jpg и 012.mp4.")
    project["scenes_synced"] = False
    project["render"] = {"status": "idle", "progress": 0, "quality": "", "error": ""}
    out = _public(_write(project))
    out["media_replaced"] = replaced
    return out


@router.post("/{project_id}/scene/{scene_id}/swap-source")
def swap_scene_source(project_id: str, scene_id: str):
    project = _read(project_id)
    scene = next((item for item in project.get("scenes", []) if item.get("id") == scene_id), None)
    if not scene or not isinstance(scene.get("alternate_source"), dict):
        raise UserError("Для этой сцены нет второго варианта.")
    current = _source_snapshot(scene)
    _activate_source(scene, scene["alternate_source"])
    scene["alternate_source"] = current
    project["render"] = {"status": "idle", "progress": 0, "quality": "", "error": ""}
    return _public(_write(project))


@router.delete("/{project_id}/audio/{kind}")
def remove_audio(project_id: str, kind: str):
    if kind not in ("voice", "music"):
        raise HTTPException(404)
    project = _read(project_id)
    project[kind] = None
    if kind == "voice":
        project["transcript"] = {"status": "idle", "text": "", "words": [], "language": "", "model": "small", "error": "", "step": ""}
        project["scenes_synced"] = False
        (_project_dir(project_id) / "render" / "subtitles.srt").unlink(missing_ok=True)
    project["render"] = {"status": "idle", "progress": 0, "quality": "", "error": ""}
    return _public(_write(project))


@router.get("/{project_id}/media/{filename}")
def source_file(project_id: str, filename: str):
    if not FILE_ID.fullmatch(filename or ""):
        raise HTTPException(404)
    path = _project_dir(project_id) / "media" / filename
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})


@router.get("/{project_id}/output/{quality}")
def output_file(project_id: str, quality: str, download: bool = False):
    if quality == "subtitles":
        path = _project_dir(project_id) / "render" / "subtitles.srt"
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, filename=f"{_read(project_id)['title']}.srt", media_type="application/x-subrip")
    if quality not in ("preview", "final"):
        raise HTTPException(404)
    path = _project_dir(project_id) / "render" / f"{quality}.mp4"
    if not path.is_file():
        raise HTTPException(404)
    headers = {"Cache-Control": "no-store"}
    return FileResponse(path, filename=f"{_read(project_id)['title']}.mp4" if download else None, headers=headers)


@router.get("/{project_id}/waveform/{kind}")
def waveform(project_id: str, kind: str, points: int = 700):
    if kind not in ("voice", "music"):
        raise HTTPException(404)
    project = _read(project_id)
    item = project.get(kind)
    if not item:
        return {"peaks": [], "duration": 0}
    path = _project_dir(project_id) / "media" / item["source"]
    p = subprocess.run([media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-t", "7200",
                        "-ac", "1", "-ar", "2000", "-f", "s16le", "-"], capture_output=True)
    if p.returncode:
        raise UserError("Не удалось построить волну звука.")
    samples = array.array("h")
    samples.frombytes(p.stdout)
    points = int(_number(points, 700, 100, 1600))
    step = max(len(samples) // points, 1)
    peaks = [round(max((abs(x) for x in samples[i:i + step]), default=0) / 32768, 3)
             for i in range(0, len(samples), step)][:points]
    return {"peaks": peaks, "duration": item.get("duration", 0)}


@router.get("/whisper/info")
def whisper_info():
    return local_whisper.info()


def _transcript_cues(project: dict) -> list:
    words = [(str(w[0]), float(w[1]), float(w[2]), 0) for w in project.get("transcript", {}).get("words", [])]
    return captions.group(words, 3, 18) if project.get("format") == "short" else captions.group(words, 8, 42)


def _write_srt(project: dict):
    cues = _transcript_cues(project)
    if cues:
        captions.write_srt(cues, _project_dir(project["id"]) / "render" / "subtitles.srt")


def _set_transcript(project_id: str, **changes):
    with _lock:
        try:
            project = _read(project_id)
        except Exception:
            return
        project.setdefault("transcript", {}).update(changes)
        _write(project)


def _transcribe_worker(project_id: str, model: str, language: str):
    try:
        project = _read(project_id)
        voice = project.get("voice")
        if not voice:
            raise UserError("Сначала добавьте озвучку.")
        source = _project_dir(project_id) / "media" / voice["source"]
        text, words, detected = local_whisper.transcribe(
            source, model, language,
            on_status=lambda step: _set_transcript(project_id, step=step),
        )
        project = _read(project_id)
        project["transcript"] = {
            "status": "done", "text": text, "words": words, "language": detected,
            "model": model, "error": "", "step": "Готово", "finished": _now(),
        }
        if project.get("scenes"):
            cuts = _sync_cuts(project)
            for scene, start, end in zip(project["scenes"], cuts, cuts[1:]):
                scene["duration"] = round(end - start, 3)
            project["scenes_synced"] = True
            if project.get("graphics_auto"):
                _apply_graphic_suggestions(project)
        _write_srt(project)
        _write(project)
    except Exception as exc:
        _set_transcript(project_id, status="error", error=str(exc), step="")
    finally:
        with _lock:
            _transcribing.discard(project_id)


@router.post("/{project_id}/transcribe")
def start_transcription(project_id: str, model: str = "small", language: str = ""):
    project = _read(project_id)
    if not project.get("voice"):
        raise UserError("Сначала добавьте озвучку.")
    if model not in local_whisper.MODELS:
        model = "small"
    with _lock:
        if project_id in _transcribing:
            raise UserError("Эта озвучка уже расшифровывается.")
        if project_id in _rendering:
            raise UserError("Сначала дождитесь окончания сборки видео.")
        _transcribing.add(project_id)
    project["transcript"] = {"status": "running", "text": "", "words": [], "language": language,
                             "model": model, "error": "", "step": "Запускаю локальный Whisper…", "started": _now()}
    _write(project)
    threading.Thread(target=_transcribe_worker, args=(project_id, model, language), daemon=True,
                     name=f"local-whisper-{project_id}").start()
    return {"ok": True, "status": "running"}


def _sync_cuts(project: dict) -> list[float]:
    scenes = project.get("scenes", [])
    words = project.get("transcript", {}).get("words", [])
    total = float((project.get("voice") or {}).get("duration") or (words[-1][2] if words else 0))
    if not scenes or not words or total <= 0:
        raise UserError("Для синхронизации нужны сцены и готовая расшифровка Whisper.")
    current = [max(float(s.get("duration") or 0), .1) for s in scenes]
    current_total = sum(current) or len(scenes)
    targets, acc = [], 0.0
    for duration in current[:-1]:
        acc += duration
        targets.append(acc / current_total * total)
    boundaries = []
    for i, word in enumerate(words[:-1]):
        end, next_start = float(word[2]), float(words[i + 1][1])
        text = str(word[0])
        if next_start - end >= .22 or re.search(r"[.!?…]\W*$", text):
            boundaries.append((end + next_start) / 2)
    cuts, last = [0.0], 0.0
    for target in targets:
        available = [x for x in boundaries if x >= last + .25 and x <= total - .25]
        cut = min(available, key=lambda x: abs(x - target)) if available else target
        if abs(cut - target) > 3.0:
            cut = target
        cut = min(max(cut, last + .25), total - .25)
        cuts.append(cut)
        last = cut
    cuts.append(total)
    return cuts


@router.post("/{project_id}/sync-scenes")
def sync_scenes(project_id: str):
    project = _read(project_id)
    cuts = _sync_cuts(project)
    for scene, start, end in zip(project["scenes"], cuts, cuts[1:]):
        scene["duration"] = round(end - start, 3)
    project["scenes_synced"] = True
    project["render"] = {"status": "idle", "progress": 0, "quality": "", "error": ""}
    return _public(_write(project))


def _graphic_suggestions(project: dict) -> list[tuple[int, dict]]:
    """Find facts and visual cues in the local Whisper transcript. No model or network call is used."""
    words = project.get("transcript", {}).get("words", [])
    scenes = project.get("scenes", [])
    if not words or not scenes:
        raise UserError("Сначала расшифруйте озвучку Whisper и добавьте сцены.")
    ends, total = [], 0.0
    for scene in scenes:
        total += float(scene.get("duration") or 0)
        ends.append(total)
    suggestions, used_scenes, last_time = [], set(), -99.0
    limit = 4 if project.get("format") == "short" else min(16, max(5, len(scenes) // 2))
    for wi, raw in enumerate(words):
        token = str(raw[0]).strip()
        match = re.search(r"(?<!\w)([+−-]?[€$₽]?\d+(?:[.,]\d+)?[%€$₽]?)(?!\w)", token)
        if not match:
            continue
        at = float(raw[1])
        if at - last_time < (4 if project.get("format") == "short" else 8):
            continue
        scene_i = next((i for i, end in enumerate(ends) if at < end), len(scenes) - 1)
        if scene_i in used_scenes:
            continue
        shown = match.group(1).replace("−", "-")
        clean = re.sub(r"[^\d.,+-]", "", shown).replace(",", ".")
        try:
            number = float(clean)
        except ValueError:
            continue
        following = []
        for next_word in words[wi + 1:wi + 4]:
            raw_next = str(next_word[0])
            cleaned_next = re.sub(r"^[\W_]+|[\W_]+$", "", raw_next)
            if cleaned_next and not re.search(r"\d", cleaned_next):
                following.append(cleaned_next)
            if re.search(r"[.!?…]", raw_next):
                break
        context = " ".join(following[:2])[:40]
        low = (token + " " + context).lower()
        if "%" in shown or "процент" in low:
            kind, unit = "percent", "%"
        elif any(x in shown for x in ("₽", "$", "€")) or any(x in low for x in ("рубл", "доллар", "евро")):
            kind = "money"
            unit = next((x for x in ("₽", "$", "€") if x in shown), "₽" if "рубл" in low else "$" if "доллар" in low else "€")
        elif number.is_integer() and 1000 <= number <= 2100:
            kind, unit = "date", ""
        elif any(x in low for x in ("место", "топ")):
            kind, unit = "rank", ""
        else:
            kind, unit = "counter", ""
        start = sum(float(s.get("duration") or 0) for s in scenes[:scene_i])
        graphic = {"type": kind, "value": clean, "unit": unit, "label": context,
                   "start": round(min(max(at - start - .15, 0), max(float(scenes[scene_i].get("duration") or 0) - .2, 0)), 2),
                   "style": ("glass", "neon", "minimal", "classic")[scene_i % 4],
                   "animation": ("pop", "slide", "pulse", "fade")[scene_i % 4], "auto": True}
        suggestions.append((scene_i, graphic))
        used_scenes.add(scene_i)
        last_time = at
        if len(suggestions) >= limit:
            break
    semantic_last = -99.0
    rules = [
        (r"опас|угроз|предупр|риск", "warning"),
        (r"вырос|рост|увелич|поднял|подорож", "up"),
        (r"упал|паден|сниз|уменьш|сократ|подешев", "down"),
        (r"люд|человек|жител|населен|аудитор", "people"),
        (r"город|страна|остров|место|регион|континент", "location"),
        (r"иде|открыт|изобрел|придум", "idea"),
        (r"побед|достижен|чемпион|награ", "trophy"),
        (r"архив|документ|доклад|письмо|записк", "document"),
        (r"секрет|засекреч|тайн", "secret"),
        (r"новост|сообщил|данным", "news"),
        (r"спустя|позже|затем", "timeline"),
        (r"главн|важн|впервые|никогда", "keyword"),
    ]
    for wi, raw in enumerate(words):
        at = float(raw[1])
        if at - semantic_last < (5 if project.get("format") == "short" else 9):
            continue
        token = re.sub(r"^[\W_]+|[\W_]+$", "", str(raw[0])).lower().replace("ё", "е")
        phrase_start, phrase_count, unit = wi, 3, ""
        if token in ("звали", "имени", "называли") and wi + 1 < len(words):
            kind, phrase_start, phrase_count, unit = "name", wi + 1, 2, "герой истории"
        elif re.search(r"сказал|сказала|говорил|заявил|ответил", token) and wi + 1 < len(words):
            kind, phrase_start, phrase_count = "quote", wi + 1, 5
        elif re.search(r"глава|часть|далее|тем временем", token):
            kind, phrase_start, phrase_count, unit = "chapter", wi + 1, 3, token
        else:
            kind = next((kind for pattern, kind in rules if re.search(pattern, token)), "")
        if not kind:
            continue
        if kind == "location":
            phrase_start, phrase_count, unit = wi + 1, 2, token
        scene_i = next((i for i, end in enumerate(ends) if at < end), len(scenes) - 1)
        if scene_i in used_scenes:
            continue
        phrase = []
        for item in words[phrase_start:phrase_start + phrase_count]:
            cleaned = re.sub(r"^[\W_]+|[\W_]+$", "", str(item[0]))
            if cleaned:
                phrase.append(cleaned)
            if re.search(r"[.!?…]", str(item[0])):
                break
        start = sum(float(s.get("duration") or 0) for s in scenes[:scene_i])
        special = kind in ("name", "location", "quote", "chapter", "document", "secret", "news", "timeline")
        graphic = {"type": kind, "value": "", "unit": unit, "label": " ".join(phrase)[:40],
                   "start": round(min(max(at - start - .1, 0), max(float(scenes[scene_i].get("duration") or 0) - .2, 0)), 2),
                   "style": ("cinematic", "broadcast")[scene_i % 2] if special else ("glass", "neon", "minimal", "classic")[scene_i % 4],
                   "animation": {"name": "slide", "location": "fade", "quote": "fade", "chapter": "pop", "document": "slide", "secret": "pop", "news": "slide", "timeline": "fade"}.get(kind, ("pop", "slide", "pulse", "fade")[scene_i % 4]), "auto": True}
        suggestions.append((scene_i, graphic))
        used_scenes.add(scene_i)
        semantic_last = at
        if len(suggestions) >= limit:
            break
    return suggestions


def _apply_graphic_suggestions(project: dict) -> int:
    added = 0
    for scene_i, graphic in _graphic_suggestions(project):
        scene = project["scenes"][scene_i]
        current = scene.get("graphic") or {}
        if current.get("type") not in (None, "", "none") and not current.get("auto"):
            continue
        scene["graphic"] = graphic
        added += 1
    return added


@router.post("/{project_id}/suggest-graphics")
def suggest_graphics(project_id: str):
    project = _read(project_id)
    added = _apply_graphic_suggestions(project)
    if not added:
        raise UserError("Подходящих фактов или визуальных акцентов не найдено. Моушен можно настроить вручную.")
    project["graphics_enabled"] = True
    project["render"] = {"status": "idle", "progress": 0, "quality": "", "error": ""}
    out = _public(_write(project))
    out["graphics_added"] = added
    return out


def _set_render(project_id: str, **changes):
    with _lock:
        try:
            project = _read(project_id)
        except Exception:
            return
        project.setdefault("render", {}).update(changes)
        _write(project)


def _fit_filter(width: int, height: int, fit: str) -> str:
    if fit == "contain":
        return (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black")
    return f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"


def _image_filter(scene: dict, width: int, height: int, fps: int, frames: int) -> str:
    fit = scene.get("fit", "cover")
    motion = scene.get("motion", "none")
    if motion == "none" or fit == "contain":
        vf = f"{_fit_filter(width, height, fit)},fps={fps}"
    else:
        big_w, big_h = width * 2, height * 2
        p = f"on/{max(frames, 1)}"
        if motion == "zoom_out":
            z, x = f"1.16-0.16*{p}", "(iw-iw/zoom)/2"
        elif motion == "pan_left":
            z, x = "1.16", f"(iw-iw/zoom)*(1-{p})"
        elif motion == "pan_right":
            z, x = "1.16", f"(iw-iw/zoom)*{p}"
        else:
            z, x = f"1+0.16*{p}", "(iw-iw/zoom)/2"
        vf = (f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase,crop={big_w}:{big_h},"
              f"zoompan=z='{z}':x='{x}':y='(ih-ih/zoom)/2':d={frames}:s={width}x{height}:fps={fps}")
    if scene.get("transition") == "fade":
        duration = frames / fps
        vf += f",fade=t=in:st=0:d=0.35,fade=t=out:st={max(duration - .35, 0):.3f}:d=0.35"
    return vf + ",setsar=1,format=yuv420p"


def _video_filter(scene: dict, width: int, height: int, fps: int) -> str:
    duration = float(scene["duration"])
    source_window = max(float(scene.get("trim_end") or duration) - float(scene.get("trim_start") or 0), .1)
    # If the voice needs this scene for longer than the source clip, hold the last frame.
    vf = (f"trim=duration={source_window:.3f},setpts=PTS-STARTPTS,{_fit_filter(width, height, scene.get('fit', 'cover'))},"
          f"fps={fps},tpad=stop_mode=clone:stop_duration={duration:.3f}")
    if scene.get("transition") == "fade":
        vf += f",fade=t=in:st=0:d=0.35,fade=t=out:st={max(duration - .35, 0):.3f}:d=0.35"
    return vf + ",setsar=1,format=yuv420p"


def _render_clip(project_dir: Path, scene: dict, out: Path, width: int, height: int, fps: int,
                 graphics_enabled: bool = False):
    source = project_dir / "media" / scene["source"]
    duration = float(scene["duration"])
    frames = max(round(duration * fps), 1)
    graphic = deepcopy(scene.get("graphic") or {})
    active_graphic = graphics_enabled and graphic.get("type") in LOCAL_GRAPHICS - {"none"}
    base = out.with_name(f".{out.stem}.base.mp4") if active_graphic else out
    if scene["kind"] == "image":
        args = ["-loop", "1", "-framerate", fps, "-i", source, "-vf", _image_filter(scene, width, height, fps, frames),
                "-frames:v", frames]
    else:
        args = ["-ss", f"{float(scene.get('trim_start') or 0):.3f}", "-i", source, "-t", f"{duration:.3f}",
                "-vf", _video_filter(scene, width, height, fps)]
    media.run([*args, "-an", "-r", fps, "-c:v", "libx264", "-preset", "veryfast", "-crf", 22,
               "-pix_fmt", "yuv420p", base], "Фрагмент локального монтажа")
    if active_graphic:
        work = out.parent / f".{out.stem}-graphics"
        if graphic["type"] == "date":
            caption = graphic.get("label", "")
            graphic["label"] = graphic.get("value", "")
            graphic["unit"] = caption
        graphic["start"] = min(max(float(graphic.get("start") or 0), 0), max(duration - .1, 0))
        try:
            graphics.overlay(base, out, [graphic], width, height, work, fps=fps)
        finally:
            base.unlink(missing_ok=True)
            shutil.rmtree(work, ignore_errors=True)


def _mix(project_dir: Path, project: dict, video: Path, out: Path, total: float):
    args: list = ["-i", video]
    filters = []
    audio_labels = []
    idx = 1
    voice = project.get("voice")
    music = project.get("music")
    if voice:
        args += ["-i", project_dir / "media" / voice["source"]]
        filters.append(f"[{idx}:a]aformat=sample_rates=44100:channel_layouts=stereo,apad,atrim=0:{total:.3f}[voice]")
        audio_labels.append("[voice]")
        idx += 1
    if music:
        args += ["-stream_loop", "-1", "-i", project_dir / "media" / music["source"]]
        volume = float(project.get("music_volume", .12))
        filters.append(f"[{idx}:a]aformat=sample_rates=44100:channel_layouts=stereo,volume={volume:.3f},atrim=0:{total:.3f}[music]")
        audio_labels.append("[music]")
    if not audio_labels:
        args += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=44100:cl=stereo"]
        audio_map = f"{idx}:a"
    elif len(audio_labels) == 1:
        audio_map = audio_labels[0]
    else:
        filters.append("".join(audio_labels) + f"amix=inputs={len(audio_labels)}:duration=longest:normalize=0[a]")
        audio_map = "[a]"
    filter_args = ["-filter_complex", ";".join(filters)] if filters else []
    media.run([*args, *filter_args, "-map", "0:v", "-map", audio_map, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
               "-t", f"{total:.3f}", "-movflags", "+faststart", out], "Звук локального монтажа")


def _burn_subtitles(project: dict, video: Path, out: Path, width: int, height: int):
    cues = _transcript_cues(project)
    if not cues:
        shutil.copy2(video, out)
        return
    render_dir = _project_dir(project["id"]) / "render"
    srt, ass = render_dir / "subtitles.srt", render_dir / "subtitles.ass"
    captions.write_srt(cues, srt)
    fonts, family = captions.font()
    vertical = height > width
    pos = project.get("subtitles_position", "mid_low")
    margin = int(height * captions.POS_SHARE[vertical].get(pos, captions.POS_SHARE[vertical]["mid_low"]))
    captions.write_ass(cues, ass, width, height, vertical, family, margin_v=margin)
    rel_fonts = str(Path(fonts).resolve()).replace("\\", "/").replace(":", "\\:")
    media.run(["-i", video.resolve(), "-vf", f"subtitles=filename={ass.name}:fontsdir={rel_fonts}",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", 20, "-pix_fmt", "yuv420p",
               "-c:a", "copy", "-movflags", "+faststart", out.resolve()],
              "Субтитры локального монтажа", cwd=render_dir, exe=media.ffmpeg_with_subtitles())


def _render_worker(project_id: str, quality: str):
    work = None
    try:
        project = _read(project_id)
        project_dir = _project_dir(project_id)
        scenes = project.get("scenes", [])
        if not scenes:
            raise UserError("Добавьте хотя бы одну картинку или видео.")
        if quality == "preview":
            width, height = ((540, 960) if project.get("format") == "short" else (960, 540))
        else:
            width, height = ((1080, 1920) if project.get("format") == "short" else (1920, 1080))
        fps = int(project.get("fps") or 30)
        work = project_dir / "render" / f".{quality}-work"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        clips = []
        for i, scene in enumerate(scenes):
            clip = work / f"{i:04d}.mp4"
            _render_clip(project_dir, scene, clip, width, height, fps, bool(project.get("graphics_enabled")))
            clips.append(clip)
            _set_render(project_id, progress=round((i + 1) / (len(scenes) + 1) * .9, 3))
        joined = work / "joined.mp4"
        media.concat_clips(clips, joined)
        total = sum(float(s["duration"]) for s in scenes)
        target = project_dir / "render" / f"{quality}.mp4"
        tmp = project_dir / "render" / f".{quality}.tmp.mp4"
        _mix(project_dir, project, joined, tmp, total)
        if project.get("subtitles") and project.get("transcript", {}).get("words"):
            burned = project_dir / "render" / f".{quality}.subtitled.tmp.mp4"
            _burn_subtitles(project, tmp, burned, width, height)
            burned.replace(target)
            tmp.unlink(missing_ok=True)
        else:
            tmp.replace(target)
        _set_render(project_id, status="done", progress=1, quality=quality, error="", finished=_now())
    except Exception as exc:  # the error must be visible on the editor page, not lost in the background thread
        _set_render(project_id, status="error", error=str(exc), progress=0, quality=quality)
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)
        with _lock:
            _rendering.discard(project_id)


@router.post("/{project_id}/render")
def start_render(project_id: str, quality: str = "preview"):
    project = _read(project_id)
    if quality not in ("preview", "final"):
        raise UserError("Неизвестное качество сборки.")
    if not project.get("scenes"):
        raise UserError("Добавьте хотя бы одну картинку или видео.")
    with _lock:
        if project_id in _rendering:
            raise UserError("Этот проект уже собирается.")
        if project_id in _transcribing:
            raise UserError("Сначала дождитесь окончания расшифровки Whisper.")
        _rendering.add(project_id)
    _set_render(project_id, status="running", progress=0, quality=quality, error="", started=_now())
    threading.Thread(target=_render_worker, args=(project_id, quality), daemon=True, name=f"local-render-{project_id}").start()
    return {"ok": True, "status": "running", "quality": quality}
