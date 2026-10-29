"""Local web server: the browser window of the app."""
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import analytics, animate, captions, cta, exporter, balance, db, effects, images, instagram, llm, media, music as musiclib, pipeline, producer, projects, remake, thumb, tts, updater, uploader, worker, youtube
from .config import (DATA_DIR, DEFAULTS, MUSIC_DIR, PROMPTS_DEFAULT_DIR, PROMPTS_DIR, load_settings,
                     public_settings, save_settings)
from .net import UserError

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Авто ютуб")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC / "favicon.ico")


@app.exception_handler(UserError)
def _user_error(request: Request, exc: UserError):
    return JSONResponse({"error": str(exc)}, status_code=400)


@app.exception_handler(Exception)
def _any_error(request: Request, exc: Exception):
    return JSONResponse({"error": f"Непредвиденная ошибка: {exc}"}, status_code=500)


@app.on_event("startup")
def _startup():
    db.init()
    worker.start()
    exporter.backfill()
    analytics.start()


@app.get("/")
def index(request: Request, code: str = "", state: str = "", error: str = ""):
    # Google sends the user back here after "Подключить канал".
    if error:
        youtube.last_error = "Вход в Google отменён." if error == "access_denied" else f"Google: {error}"
        return RedirectResponse("/#/settings/youtube")
    if code and state:
        try:
            ch = youtube.finish(code, state)
            youtube.last_error = ""
            return RedirectResponse(f"/#/settings/youtube-ok")
        except UserError as e:
            youtube.last_error = str(e)
            return RedirectResponse("/#/settings/youtube")
    # Version marks on the scripts make the browser load the new ones right after an update.
    v = int(max((STATIC / n).stat().st_mtime for n in ("app.js", "style.css")))
    html = (STATIC / "index.html").read_text("utf-8")
    html = html.replace("/static/app.js", f"/static/app.js?v={v}").replace("/static/style.css", f"/static/style.css?v={v}")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/api/backup")
def backup_list():
    from . import backup
    return {"folder": str(backup.BACKUP_DIR), "items": [{"name": f.stem, "size": f.stat().st_size} for f in backup.backups()]}


@app.post("/api/backup")
def backup_make():
    from . import backup
    out = backup.make()
    where = "на рабочий стол" if out.parent == backup.BACKUP_DIR else f"в папку {out.parent} (macOS не дал доступ к рабочему столу)"
    return {"name": out.stem, "where": where}


@app.post("/api/shutdown")
def shutdown():
    import os
    import threading
    threading.Timer(0.5, lambda: os._exit(0)).start()
    return {"ok": True}


@app.get("/api/meta")
def meta():
    return {
        "stages": [{"id": s, "name": n} for s, n, _ in pipeline.STAGES],
        "criteria": pipeline.CRITERIA_RU,
        "providers": {k: {**v, "models": [{"id": m, "name": n, "price": p} for m, n, p in v["models"]]}
                      for k, v in images.PROVIDERS.items()},
        "tts_models": [{"id": m, "name": n} for m, n in tts.MODELS],
        "music_modes": musiclib.MODES, "music_volume": float(load_settings().get("music_volume", 0.12)),
        "video_models": [{"id": k, "name": v["name"], "price": v["price"],
                          "clip": round(animate.clip_seconds({"video_model": k}, 5) * v["price"], 2)}
                         for k, v in animate.VIDEO_MODELS.items()],
        "anim_modes": animate.MODES, "video_model": load_settings().get("video_model", "kling25"),
        "fx_modes": effects.MODES, "subs_positions": captions.POSITIONS, "cta_modes": cta.MODES, "cta_langs": cta.LANGS, "thumb_fonts": {k: v[0] for k, v in thumb.STYLE_FONTS.items()}, "thumb_looks": thumb.LOOKS, "effect_ru": effects.EFFECT_RU, "sfx_ru": effects.SFX_RU,
        "image_styles": projects.IMAGE_STYLES,
        "default_niches": {**{k: v.get("niche", "") for k, v in projects.PRESETS.items()}, "": DEFAULTS["niche"]},
        "presets": {k: {"name": v["name"], "icon": v["icon"], "language": v["language"]} for k, v in projects.PRESETS.items()},
        "languages": projects.LANGUAGES,
        "data_dir": str(DATA_DIR),
    }


# ---------------------------------------------------------------- settings

@app.get("/api/settings")
def get_settings():
    return public_settings()


@app.post("/api/settings")
def post_settings(data: dict = Body(...)):
    save_settings(data)
    return public_settings()


@app.get("/api/estimate")
def estimate(minutes: float = 10, short: bool = True, anim_mode: str = "off", anim_short: bool = False,
             language: str = "en", format: str = "long", short_seconds: float = 50, music_mode: str = "library",
             anim_count: int = animate.COUNT_DEFAULT):
    s = load_settings()
    if format == "short":
        minutes, short, anim_short = _short_seconds(short_seconds) / 60, False, False
    words = minutes * pipeline.WPM.get(language, pipeline.WORDS_PER_MIN)
    scenes = words / float(s["short_words_per_scene" if format == "short" else "words_per_scene"])
    n_images = scenes + (11 if short else 0) + 1
    img_cost = n_images * images.price_per_image(s)
    model = s["anthropic_model"] if s["llm_provider"] == "anthropic" else s["openai_model"]
    pin, pout = llm.PRICES.get(model, (3.0, 15.0))
    # ~ 9 calls; output dominated by script + 3-4 rewrites + translation
    tokens_out = words * 1.4 * 7 + scenes * 80
    tokens_in = words * 1.4 * 10 + 20000
    llm_cost = tokens_in / 1e6 * pin + tokens_out / 1e6 * pout
    chars = words * (5.8 if language != "ru" else 7.2) + (800 if short else 0)
    anim_cost = animate.estimate(s, anim_mode, minutes, short, anim_short, format == "short", anim_count)
    music_cost = musiclib.cost(musiclib.length_for(minutes * 60, format == "short")) if music_mode == "generate" else 0
    return {"images": int(n_images), "image_cost": round(img_cost, 2), "llm_cost": round(llm_cost, 2),
            "anim_cost": anim_cost, "music_cost": music_cost, "tts_chars": int(chars),
            "total": round(img_cost + llm_cost + anim_cost + music_cost, 2)}


# ---------------------------------------------------------------- jobs

@app.get("/api/jobs")
def jobs(project: str = ""):
    out = []
    titles = {c["id"]: c["title"] for c in youtube.list_channels()}
    projs = {p["id"]: p for p in projects.list_all()}
    for j in db.list_all():
        pid = j["options"].get("project_id", "")
        if project and (pid if pid in projs else "none") != project:
            continue
        j["project"] = {k: projs[pid][k] for k in ("id", "name", "icon")} if pid in projs else None
        j["channel_title"] = titles.get(j["options"].get("channel_id", ""), "")
        d = pipeline.job_dir(j["id"])
        j["has_video"] = (d / "output" / "video.mp4").exists()
        j["thumb"] = next((f"output/{n}" for n in ("thumbnail.jpg", "thumbnail.png") if (d / "output" / n).exists()), "")
        j["stage_name"] = pipeline.STAGE_NAMES.get(j["stage"], "Готово" if j["stage"] == "done" else "")
        out.append(j)
    return out


def _short_seconds(v) -> int:
    try:
        return int(min(max(float(v), 15), 180))
    except (TypeError, ValueError):
        return 50


def _job_niche(sent, own: str) -> str:
    """The niche typed on the new-video page wins; if it is the project's own niche with its line breaks lost
    (older pages showed it in a one-line field), the original text with its structure is kept."""
    sent = (sent or "").strip()
    if not sent or "".join(sent.split()) == "".join((own or "").split()):
        return own
    return sent


@app.post("/api/jobs")
def create_job(data: dict = Body(...)):
    s = load_settings()
    count = max(1, min(int(data.get("count") or 1), 20))
    topic = (data.get("topic") or "").strip()
    proj = projects.get((data.get("project_id") or "").strip())
    fmt = data.get("format") if data.get("format") in ("long", "short") else proj.get("format", "long")
    opts = {
        "project_id": proj.get("id", ""),
        "language": data.get("language") if data.get("language") in projects.LANGUAGES else proj.get("language", "en"),
        "format": fmt,
        "make_short": bool(data.get("make_short", proj.get("make_short", True))),
        "voice_off": bool(data.get("voice_off", proj.get("voice_off", False))),  # music only, the text on screen
        "review_pause": bool(data.get("review_pause", False)),
        "idea_pause": bool(data.get("idea_pause", False)),
        "duration_min": float(data.get("duration_min") or proj.get("duration_min") or s["duration_min"]),
        "niche": _job_niche(data.get("niche"), proj.get("niche") or _channel_niche(data, proj)
                            or (proj.get("name", "") if proj.get("id") else s["niche"])),
        "demo": bool(data.get("demo", False)),
        # "" means "Без канала" was picked on purpose; only a missing key falls back to the project's channel.
        "channel_id": _job_channel(data, proj),
        "subs_video": bool(data.get("subs_video", proj.get("subs_short" if fmt == "short" else "subs_video",
                                                        s.get("subtitles_short" if fmt == "short" else "subtitles_long")))),
        "subs_short": bool(data.get("subs_short", proj.get("subs_short", s.get("subtitles_short")))),
        "graphics": bool(data.get("graphics", proj.get("graphics", True))),
        "subs_smart": bool(data.get("subs_smart", proj.get("subs_smart", True))),
        "anim_mode": (data.get("anim_mode") if data.get("anim_mode") in animate.MODES
                      else proj.get("anim_mode") or s.get("anim_mode", "off")),
        "anim_short": bool(data.get("anim_short", proj.get("anim_short", s.get("anim_short")))),
        "anim_count": animate.clamp_count(data.get("anim_count") or proj.get("anim_count") or s.get("anim_count") or animate.COUNT_DEFAULT),
        "anim_pause": bool(data.get("anim_pause", proj.get("anim_pause", False))),
        "images_pause": bool(data.get("images_pause", proj.get("images_pause", False))),
        "fx_mode": data.get("fx_mode") if data.get("fx_mode") in effects.MODES else proj.get("fx_mode") or s.get("fx_mode", "light"),
        "short_seconds": _short_seconds(data.get("short_seconds") or proj.get("short_seconds")),
        "music_mode": data.get("music_mode") if data.get("music_mode") in musiclib.MODES else proj.get("music_mode") or "library",
    }
    voice_up = _upload_path(data.get("own_voice_upload"))
    video_up = _upload_path(data.get("own_video_upload"))
    if data.get("polish"):
        # A finished video only polished: the voice is its own sound unless a separate recording was given.
        if not video_up:
            raise UserError("Загрузите готовый ролик.")
        opts["polish"] = True
        opts["anim_mode"] = "off"
        # The words and their times come from the sound itself, or from the owner's text laid over its pauses.
        data["montage_script"] = data.get("montage_script") or " "
        if not voice_up:
            voice_up = video_up.with_name(video_up.stem + "_voice.m4a")
            try:
                media.run(["-i", video_up, "-vn", "-ac", 1, "-c:a", "aac", "-b:a", "128k", voice_up], "Звук из ролика")
            except UserError:
                raise UserError("В ролике нет звука. Загрузите озвучку отдельным файлом.")
    if voice_up:
        data["montage_script"] = data.get("montage_script") or " "  # no text: it is transcribed from the recording
    if data.get("music_track") and (MUSIC_DIR / Path(str(data["music_track"])).name).is_file():
        opts["music_track"] = Path(str(data["music_track"])).name  # the owner's own track, uploaded on the montage page
    if data.get("montage_script"):  # the montage page: one video from the owner's own text, voice chosen there
        opts["make_short"] = False
        for k in projects.VOICE_NUM:
            if data.get(k) not in (None, ""):
                opts[k] = float(data[k])
        if data.get("voice_id"):
            opts["voice_id"], opts["voice_name"] = data["voice_id"], data.get("voice_name", "")
        if data.get("tts_model"):
            opts["tts_model"] = data["tts_model"]
        if data.get("speed_video"):
            opts["speed_video"] = min(pipeline.SPEEDS, key=lambda x: abs(x - float(data["speed_video"])))
        for k, allowed in (("cta_short", cta.MODES), ("cta_lang", cta.LANGS)):
            if data.get(k) in allowed:
                opts[k] = data[k]
        count = 1
    chars = projects.list_chars(set(data.get("characters", proj.get("characters", [])) or []))
    opts["characters"] = [c["id"] for c in chars]
    items = projects.list_items(set(data.get("items", proj.get("items", [])) or []))
    opts["items"] = [it["id"] for it in items]
    if proj.get("ad_mode") and not items:
        raise UserError("Это рекламный проект: выберите товар в блоке «Предметы в ролике» (или добавьте его в разделе «Предметы» с фото).")
    created = []
    # A montage video is the owner's own text, not the next episode of the series.
    first_ep = _next_episode(proj) if proj.get("series") and not data.get("montage_script") else 0
    if first_ep and str(data.get("episode") or "").strip().isdigit():
        first_ep = max(1, int(data["episode"]))
    for n in range(count):
        if first_ep:
            opts["episode"] = first_ep + n
        # Paused until its files are copied: the worker must not start a job with no characters or text yet.
        created.append(db.create(topic, dict(opts), status="paused"))
    for j in created:
        # The job keeps its own copy, so later edits of a character don't change a video in progress.
        if chars:
            jd = pipeline.job_dir(j["id"])
            (jd / "chars").mkdir(parents=True, exist_ok=True)
            snap = []
            for c in chars:
                src = projects.portrait_path(c["id"])
                if src.exists():
                    shutil.copy(src, jd / "chars" / f"{c['id']}.png")
                refs = []
                for i, r in enumerate(projects.list_refs(c["id"])):
                    shutil.copy(r, jd / "chars" / f"{c['id']}_ref{i}.png")
                    refs.append(f"chars/{c['id']}_ref{i}.png")
                snap.append({k: c.get(k, "") for k in ("id", "name", "name_en", "role", "description_ru", "appearance", "personality", "extra_ru", "speech_ru", "voice_id", "voice_name")}
                            | {"portrait": f"chars/{c['id']}.png" if src.exists() else "", "refs": refs})
            pipeline.write_json(jd / "characters.json", snap)
        if items:
            jd = pipeline.job_dir(j["id"])
            (jd / "items").mkdir(parents=True, exist_ok=True)
            snap = []
            for it in items:
                photos = []
                for i, src in enumerate([projects.item_photo_path(it["id"])] + projects.list_item_refs(it["id"])):
                    if src.exists():
                        shutil.copy(src, jd / "items" / f"{it['id']}_{i}.png")
                        photos.append(f"items/{it['id']}_{i}.png")
                snap.append({k: it.get(k, "") for k in ("id", "name", "name_en", "kind", "description_ru", "appearance", "extra_ru")}
                            | {"photos": photos})
            pipeline.write_json(jd / "items.json", snap)
    if data.get("montage_script"):
        for j in created:
            if voice_up:
                pipeline.job_dir(j["id"]).mkdir(parents=True, exist_ok=True)
                shutil.copy(voice_up, pipeline.job_dir(j["id"]) / f"own_voice{voice_up.suffix}")
            pics = [p for p in (_upload_path(x) for x in data.get("own_pics_uploads") or []) if p]
            if pics:  # the owner's pictures in his order: the script is laid over them, one scene each
                pdir = pipeline.job_dir(j["id"]) / "own_pics"
                pdir.mkdir(parents=True, exist_ok=True)
                for k, p in enumerate(pics):
                    shutil.copy(p, pdir / f"{k:03d}{p.suffix}")
            if video_up:
                pipeline.job_dir(j["id"]).mkdir(parents=True, exist_ok=True)
                shutil.copy(video_up, pipeline.job_dir(j["id"]) / f"own_video{video_up.suffix}")
            try:
                pipeline.prepare_montage(j["id"], topic, data["montage_script"])
            except UserError:
                for x in created:
                    db.delete(x["id"])
                raise
    for j in created:
        db.update(j["id"], status="queued")
    created = [db.get(j["id"]) for j in created]
    worker.wake()
    return created


UPLOADS = DATA_DIR / "uploads"
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus", ".webm", ".mp4", ".mov",
             ".png", ".jpg", ".jpeg", ".webp", ".heic", ".avif")


def _upload_path(uid):
    """A file the montage page uploaded before creating the video (see /api/uploads), or None."""
    if not uid or not re.fullmatch(r"[0-9a-f]{32}\.[a-z0-9]{2,5}", str(uid)):
        return None
    p = UPLOADS / str(uid)
    return p if p.is_file() else None


@app.post("/api/uploads")
async def upload_file(request: Request, name: str):
    """The owner's recording for the montage page, kept until the video is created."""
    ext = Path(name).suffix.lower()
    if ext not in AUDIO_EXT:
        raise UserError("Подходят звук mp3, wav, m4a, aac, ogg, flac, видео mp4, mov, webm или картинки png, jpg, webp.")
    raw = await request.body()
    if len(raw) > 2048 * 1024 * 1024:
        raise UserError("Файл больше 2 ГБ.")
    UPLOADS.mkdir(parents=True, exist_ok=True)
    for old in UPLOADS.glob("*"):  # older uploads that never became a video
        if old.stat().st_mtime < time.time() - 2 * 86400:
            old.unlink(missing_ok=True)
    import uuid
    uid = uuid.uuid4().hex + ext
    (UPLOADS / uid).write_bytes(raw)
    return {"id": uid}


def _job_channel(data: dict, proj: dict) -> str:
    if "channel_id" in data:
        return (data.get("channel_id") or "").strip()
    return proj.get("channel_id", "")


def _channel_niche(data: dict, proj: dict) -> str:
    cid = _job_channel(data, proj)
    return (youtube.get_profile(cid).get("niche") or "").strip() if cid else ""


def _next_episode(proj: dict) -> int:
    nums = [e.get("num", 0) for e in projects.list_episodes(proj["id"])]
    nums += [int((j.get("options") or {}).get("episode") or 0) for j in db.list_all()
             if (j.get("options") or {}).get("project_id") == proj["id"]]
    return max(nums, default=0) + 1


def _job_or_404(jid):
    j = db.get(jid)
    if not j:
        raise HTTPException(404, "Ролик не найден")
    return j


@app.get("/api/jobs/{jid}")
def job_detail(jid: str):
    j = _job_or_404(jid)
    d = pipeline.job_dir(jid)
    stages = {}
    for sid in pipeline.STAGE_IDS:
        stages[sid] = pipeline.read_json(d / f"{sid}.json")
    if not stages["review"]:
        prog = pipeline.read_json(d / "review_progress.json")
        if prog:
            stages["review_progress"] = prog
    log = (d / "log.txt").read_text("utf-8")[-15000:] if (d / "log.txt").exists() else ""
    imgs = sorted(p.name for p in (d / "images").glob("*.png")) if (d / "images").exists() else []
    return {
        "job": j,
        "stages": stages,
        "usage": pipeline.read_json(d / "usage.json", {}),
        "thumb": pipeline.read_json(d / "thumb.json", {}) or {},
        "log": log,
        "images": imgs,
        "own_images": pipeline.read_json(d / "own_images.json", []) or [],
        "own_clips": pipeline.read_json(d / "own_clips.json", []) or [],
        "subs_pos": (j.get("options") or {}).get("subs_pos") or load_settings().get("subs_pos") or "mid_low",
        "images_waiting": pipeline.needs_images_choice(j),
        "images_manual": pipeline.images_manual(j),
        "improve": pipeline.read_json(d / "improve.json"),
        "chapters": pipeline.chapters_text(jid),
        "files": {
            "video": (d / "output" / "video.mp4").exists(),
            "short": (d / "output" / "short.mp4").exists(),
            "thumbnail": (d / "output" / "thumbnail.jpg").exists(),
            "thumbnail_src": (d / "images" / "thumbnail_src.png").exists(),
            "youtube_txt": (d / "output" / "youtube.txt").exists(),
            "narration": (d / "narration.wav").exists(),
            "srt": (d / "output" / "subtitles.srt").exists(),
            "short_srt": (d / "output" / "short_subtitles.srt").exists(),
        },
        "subs": {"video": pipeline.subs_on(j, "video"), "short": pipeline.subs_on(j, "short")},
        "format": j["options"].get("format", "long"),
        "fx_mode": pipeline.fx_mode(j),
        "project": projects.get(j["options"].get("project_id", "")) or None,
        "anim": {"mode": pipeline.anim_options(j)[0], "short": pipeline.anim_options(j)[1],
                 "waiting": pipeline.needs_anim_choice(j), "suggest": pipeline.anim_suggest(jid, j),
                 "count": pipeline.anim_count(j), "scenes": pipeline.anim_scenes(j),
                 "files": sorted(p.name for p in (d / "anim").glob("*.mp4")) if (d / "anim").exists() else []},
        "upload": uploader.state(jid),
        "instagram": load_settings().get("ig_username", "") if load_settings().get("ig_token") else "",
        "stats": pipeline.read_json(d / "stats.json", {}) or {},
        "channels": youtube.list_channels(),
        "enabled": {s: (s not in ("short_script", "short_video")
                        or (j["options"].get("make_short") and j["options"].get("format") != "short"))
                    for s in pipeline.STAGE_IDS},
    }


@app.post("/api/jobs/{jid}/action")
def job_action(jid: str, data: dict = Body(...)):
    j = _job_or_404(jid)
    action = data.get("action")
    if action == "pause":
        if j["status"] in ("running", "queued"):
            db.update(jid, status="paused")
    elif action in ("resume", "retry"):
        db.update(jid, status="queued", error="")
    elif action == "approve":
        db.update(jid, approved=1, status="queued")
    elif action == "restart_from":
        stage = data.get("stage")
        if stage not in pipeline.STAGE_IDS:
            raise HTTPException(400, "Неизвестный шаг")
        if (j.get("options") or {}).get("montage") and stage in ("idea", "outline", "script", "review", "approval"):
            raise HTTPException(400, "Это монтаж по вашему тексту: сценарист здесь не работает. Чтобы поменять текст, начните новый монтаж.")
        # A running job is stopped first; the worker redoes it from this step once the current call returns.
        if not worker.reset_when_stopped(jid, stage):
            pipeline.reset_from(jid, stage)
            db.update(jid, status="queued", error="")
    elif action == "delete":
        db.delete(jid)
        shutil.rmtree(pipeline.job_dir(jid), ignore_errors=True)
        return {"ok": True}
    else:
        raise HTTPException(400, "Неизвестное действие")
    worker.wake()
    return db.get(jid)


@app.post("/api/jobs/{jid}/idea")
def job_idea(jid: str, data: dict = Body(...)):
    """Owner's choice of the topic out of the proposed ones."""
    j = _job_or_404(jid)
    if j["options"].get("montage"):
        raise UserError("Это монтаж по вашему тексту: темы здесь не выбираются.")
    if j["status"] == "running":
        raise UserError("Подождите, пока шаг доделается, или поставьте ролик на паузу.")
    pipeline.choose_idea(jid, int(data.get("index", -1)))
    db.update(jid, status="queued", error="")
    worker.wake()
    return {"ok": True}


@app.post("/api/jobs/{jid}/script")
def job_script(jid: str, data: dict = Body(...)):
    """Owner's manual edit of the final text; everything after it is made again from this text."""
    j = _job_or_404(jid)
    if j["status"] == "running":
        raise UserError("Подождите, пока шаг доделается, или поставьте ролик на паузу.")
    pipeline.set_final_script(jid, data.get("text") or "")
    db.update(jid, status="queued", error="")
    worker.wake()
    return {"ok": True}


@app.post("/api/jobs/{jid}/chapters")
def job_chapters(jid: str):
    """Timestamps for the description of a video made before they existed (or made again)."""
    _job_or_404(jid)
    pipeline.make_chapters(jid)
    pipeline.make_package(jid)
    text = pipeline.chapters_text(jid)
    if not text:
        raise UserError("Ролик слишком короткий для тайм-кодов: YouTube показывает главы, если их хотя бы три и каждая не короче 10 секунд.")
    return {"text": text}


@app.post("/api/jobs/{jid}/improve")
def job_improve(jid: str, data: dict = Body(...)):
    """Suggested fixes of only the weak spots of the final text; nothing changes until the owner applies them."""
    _job_or_404(jid)
    return pipeline.improve_script(jid, data.get("note") or "")


def _fix_prompt(prompt: str, fix_ru: str) -> str:
    """The owner says in Russian what is wrong with a picture; the English description is corrected for him."""
    system = "You edit prompts for an image model. Answer only with JSON."
    user = ("Here is the English description of a picture and the owner's correction in Russian. Rewrite the "
            "description so the picture follows the correction exactly. Keep everything else (people's looks, place, "
            "light, camera) unchanged. State positions and actions explicitly, the image model takes words literally. "
            f"Do not mention any art style.\n\nDESCRIPTION:\n{prompt}\n\nCORRECTION (Russian):\n{fix_ru}")
    r = llm.call_json(load_settings(), system, user, {"type": "object", "properties": {"prompt": {"type": "string"}}},
                      {}, 2000)
    return (r.get("prompt") or "").strip() or prompt


@app.post("/api/jobs/{jid}/thumb_prompt")
def job_thumb_prompt(jid: str, data: dict = Body(...)):
    """The owner's own thumbnail: his idea in Russian (turned into a stop-scroll prompt) or the prompt itself, and
    optionally one of this video's pictures as the example. Only the thumbnail picture is drawn again."""
    j = _job_or_404(jid)
    if j["status"] == "running":
        raise UserError("Подождите, пока ролик доделается, или поставьте его на паузу.")
    d = pipeline.job_dir(jid)
    ex = pipeline.read_json(d / "extras.json")
    if not ex:
        raise UserError("Превью появится после шага «Перевод и описание для YouTube».")
    prompt = (data.get("prompt") or "").strip() or ex.get("thumbnail_prompt", "")
    idea = (data.get("idea_ru") or "").strip()
    ref = Path(str(data.get("ref") or "")).name
    if ref and not re.fullmatch(r"(scene|short)_\d{3}\.png", ref) or ref and not (d / "images" / ref).is_file():
        raise UserError("Такой картинки в ролике нет.")
    if idea:
        script = (ex.get("script_ru") or "")[:6000]
        r = llm.call_json(load_settings(), "You write prompts for YouTube thumbnails. Answer only with JSON.",
                          "Write an English image prompt (40-70 words) for this video's thumbnail from the owner's idea "
                          "(Russian). Start with the main subject and the extreme emotion on the face in close-up, then "
                          "the shocking or absurd detail, then the setting; empty space on one side for the headline; no "
                          "text or letters in the image; no art style words." + pipeline.THUMB_RULE
                          + (" The owner picked an example frame of this video: the prompt keeps its characters and "
                             "setting but in a new, far more dramatic close-up composition." if ref else "")
                          + f"\n\nTITLE: {ex.get('youtube_title_ru') or ex.get('youtube_title', '')}\nCURRENT PROMPT: "
                          f"{prompt}\nTHE OWNER'S IDEA (Russian): {idea}\nSCRIPT (Russian, for facts):\n{script}",
                          {"type": "object", "properties": {"prompt": {"type": "string"}}}, {}, 3000)
        prompt = (r.get("prompt") or "").strip() or prompt
    ex["thumbnail_prompt"] = prompt
    ex["thumbnail_ref"] = ref
    ex["thumbnail_draw"] = True  # asked by the owner: drawn even when the scene pictures are made by hand
    if idea:
        ex["thumbnail_idea_ru"] = idea
    pipeline.write_json(d / "extras.json", ex)
    pipeline.regenerate_image(jid, "thumbnail", 0, None)
    if j["status"] in ("done", "error", "paused", "review"):
        db.update(jid, status="queued", error="")
        worker.wake()
    return {"ok": True, "prompt": prompt}


@app.post("/api/jobs/{jid}/image")
def job_image(jid: str, data: dict = Body(...)):
    j = _job_or_404(jid)
    if j["status"] == "running":
        raise UserError("Подождите, пока ролик доделается, или поставьте его на паузу.")
    prompt = (data.get("prompt") or "").strip() or None
    fix = (data.get("fix_ru") or "").strip()
    if fix and prompt:
        prompt = _fix_prompt(prompt, fix)
    pipeline.regenerate_image(jid, data.get("kind", "scene"), int(data["index"]), prompt)
    # A video waiting for the owner (for example before animation) redraws right away and stops at the same place.
    if j["status"] in ("done", "error", "paused", "review"):
        db.update(jid, status="queued", error="")
        worker.wake()
    return {"ok": True}


@app.post("/api/jobs/{jid}/own-image")
async def job_own_image(jid: str, request: Request, kind: str = "scene", index: int = 0):
    """The owner's own picture for a scene (made elsewhere). The video is then assembled with it."""
    j = _job_or_404(jid)
    if j["status"] == "running":
        raise UserError("Подождите, пока ролик доделается, или поставьте его на паузу.")
    raw = await request.body()
    if len(raw) > 40 * 1024 * 1024:
        raise UserError("Картинка больше 40 МБ.")
    pipeline.own_image(jid, kind if kind in ("short", "thumbnail") else "scene", int(index), raw)
    return {"ok": True}


@app.post("/api/jobs/{jid}/own-clip")
async def job_own_clip(jid: str, request: Request, kind: str = "scene", index: int = 0, ext: str = "mp4"):
    """The owner's own animated clip for a scene, made elsewhere from the scene picture."""
    j = _job_or_404(jid)
    if j["status"] == "running":
        raise UserError("Подождите, пока ролик доделается, или поставьте его на паузу.")
    raw = await request.body()
    if len(raw) > 300 * 1024 * 1024:
        raise UserError("Клип больше 300 МБ.")
    pipeline.own_clip(jid, "short" if kind == "short" else "scene", int(index), raw, "." + ext.lower()[:5])
    return {"ok": True}


@app.post("/api/jobs/{jid}/own-video")
async def job_own_video(jid: str, request: Request, ext: str = "mp4"):
    """The owner's finished video of all scenes glued together: its shots become the scenes, fitted to the voice."""
    j = _job_or_404(jid)
    if j["status"] == "running":
        raise UserError("Подождите, пока шаг доделается, или поставьте ролик на паузу.")
    raw = await request.body()
    if len(raw) > 2048 * 1024 * 1024:
        raise UserError("Видео больше 2 ГБ.")
    pipeline.own_video(jid, raw, "." + ext.lower()[:5])
    db.update(jid, status="queued", error="", stage="scenes")
    worker.wake()
    return {"ok": True}


@app.post("/api/jobs/{jid}/own-images-done")
def job_own_images_done(jid: str):
    """After a pause for own pictures (or after replacing pictures): missing ones are drawn, the video is assembled."""
    j = _job_or_404(jid)
    if j["status"] == "running":
        raise UserError("Ролик уже собирается.")
    opts = dict(j["options"], images_picked=True)
    db.update(jid, options=opts, status="queued", error="")
    worker.wake()
    return {"ok": True}


@app.get("/api/jobs/{jid}/scene-prompts")
def job_scene_prompts(jid: str):
    """Prompts for drawing the scene pictures elsewhere (shown in a window with copy buttons)."""
    _job_or_404(jid)
    return pipeline.scene_prompts(jid)


@app.post("/api/jobs/{jid}/thumbnail")
def job_thumbnail(jid: str, data: dict = Body(...)):
    """Re-draw the headline on the thumbnail. No API calls, free."""
    _job_or_404(jid)
    if not (pipeline.job_dir(jid) / "images" / "thumbnail_src.png").exists():
        raise UserError("Картинки для превью ещё нет: она появится после шага «Картинки».")
    pipeline.make_package(jid, data.get("text"), data.get("side"), data.get("font"), data.get("look"))
    return {"ok": True}


@app.post("/api/jobs/{jid}/subtitles")
def job_subtitles(jid: str, data: dict = Body(...)):
    """Switch burned-in subtitles on or off. Only the final assembly is redone: no API calls, free."""
    j = _job_or_404(jid)
    kind = data.get("kind", "video")
    if kind not in ("video", "short"):
        raise HTTPException(400, "Неизвестный тип ролика")
    d = pipeline.job_dir(jid)
    stage, out = ("video", "video.mp4") if kind == "video" else ("short_video", "short.mp4")
    if j["status"] == "running" and (j["stage"] == stage or (d / f"{stage}.json").exists()):
        raise UserError("Подождите, пока ролик доделается, или поставьте его на паузу.")
    opts = dict(j["options"])
    if data.get("pos") in captions.POSITIONS:  # only the height of the subtitles changes
        opts["subs_pos"] = data["pos"]
    else:
        opts["subs_video" if kind == "video" else "subs_short"] = bool(data.get("on"))
    db.update(jid, options=opts)
    if (d / f"{stage}.json").exists():
        # Clips stay, so only the last assembly runs again.
        (d / f"{stage}.json").unlink(missing_ok=True)
        (d / "output" / out).unlink(missing_ok=True)
        if j["status"] in ("done", "error", "paused"):
            db.update(jid, status="queued", error="")
            worker.wake()
    return {"ok": True}


@app.post("/api/jobs/{jid}/animation")
def job_animation(jid: str, data: dict = Body(...)):
    """Change which scenes are animated. Already animated scenes are kept and not paid for again."""
    j = _job_or_404(jid)
    if j["status"] == "running":
        raise UserError("Подождите, пока ролик доделается, или поставьте его на паузу.")
    mode = data.get("mode")
    if mode not in animate.MODES:
        raise HTTPException(400, "Неизвестный режим")
    # Animation is the most expensive part: ask before ordering new paid clips.
    count = animate.clamp_count(data.get("count", pipeline.anim_count(j)))
    custom = [int(i) for i in (data.get("scenes") if data.get("scenes") is not None else pipeline.anim_scenes(j))]
    if mode == "custom" and not custom:
        raise UserError("Отметьте галочками сцены, которые нужно оживить.")
    quote = pipeline.anim_quote(jid, mode, bool(data.get("short")), count, custom)
    if quote["new"] and not data.get("confirm"):
        return {"ok": False, "confirm": True, **quote}
    waiting = pipeline.needs_anim_choice(j)
    opts = dict(j["options"], anim_mode=mode, anim_short=bool(data.get("short")), anim_count=count, anim_scenes=custom)
    if waiting:
        opts["anim_picked"] = True  # the owner chose the scenes during the pause: go on with the video
    db.update(jid, options=opts)
    d = pipeline.job_dir(jid)
    for rel in ("animate.json", "video.json", "short_video.json", "output/video.mp4", "output/short.mp4",
                "clips", "clips_short"):
        pipeline._remove(d, rel)
    if j["status"] in ("done", "error", "paused") or waiting:
        db.update(jid, status="queued", error="")
        worker.wake()
    return {"ok": True}


@app.post("/api/jobs/{jid}/effects")
def job_effects(jid: str, data: dict = Body(...)):
    """Change the effects mode. Only the video assembly is redone: free, no API calls."""
    j = _job_or_404(jid)
    if j["status"] == "running":
        raise UserError("Подождите, пока ролик доделается, или поставьте его на паузу.")
    mode = data.get("mode")
    if mode not in effects.MODES:
        raise HTTPException(400, "Неизвестный режим")
    db.update(jid, options=dict(j["options"], fx_mode=mode))
    d = pipeline.job_dir(jid)
    for rel in ("video.json", "short_video.json", "output/video.mp4", "output/short.mp4", "clips", "clips_short"):
        pipeline._remove(d, rel)
    if j["status"] in ("done", "error", "paused"):
        db.update(jid, status="queued", error="")
        worker.wake()
    return {"ok": True}


@app.post("/api/jobs/{jid}/assembly")
def job_assembly(jid: str, data: dict = Body(...)):
    """Data animations and smart caption places on or off. Only the assembly is redone: free (the animation plan is
    asked once, a few cents)."""
    j = _job_or_404(jid)
    if j["status"] == "running":
        raise UserError("Подождите, пока ролик доделается, или поставьте его на паузу.")
    opts = dict(j["options"])
    for k in ("graphics", "subs_smart"):
        if k in data:
            opts[k] = bool(data[k])
    db.update(jid, options=opts)
    d = pipeline.job_dir(jid)
    for rel in ("video.json", "short_video.json", "output/video.mp4", "output/short.mp4"):
        pipeline._remove(d, rel)
    if j["status"] in ("done", "error", "paused"):
        db.update(jid, status="queued", error="")
        worker.wake()
    return {"ok": True}


@app.post("/api/jobs/{jid}/music")
def job_music(jid: str, data: dict = Body(...)):
    """The owner's own background track (or none, or automatic) and its volume. Only the assembly is redone: free."""
    j = _job_or_404(jid)
    if j["status"] == "running":
        raise UserError("Подождите, пока ролик доделается, или поставьте его на паузу.")
    opts = dict(j["options"])
    track = str(data.get("track") or "")
    if track and track != "none" and not (MUSIC_DIR / Path(track).name).is_file():
        raise UserError("Такого трека нет в разделе «Музыка».")
    opts["music_track"] = Path(track).name if track not in ("", "none") else track
    if "style" in data:  # the owner's own words about the music for this video
        opts["music_style"] = str(data.get("style") or "").strip()[:300]
    if data.get("compose"):  # compose a new track in that style
        opts["music_track"] = ""
        opts["music_mode"] = "generate"
        pipeline._remove(pipeline.job_dir(jid), "music.mp3")
        track = ""
    if not track:
        opts.pop("music_picked", None)  # automatic again: pick anew
        if opts.get("music_mode") == "off" and not data.get("compose"):
            opts["music_mode"] = "library"  # the video was made without music: "automatic" means from the library now
    if data.get("volume") not in (None, ""):
        try:
            opts["music_volume"] = min(0.6, max(0.0, float(data["volume"])))
        except (TypeError, ValueError):
            raise UserError("Громкость должна быть числом.")
    db.update(jid, options=opts)
    d = pipeline.job_dir(jid)
    for rel in ("video.json", "short_video.json", "output/video.mp4", "output/short.mp4", "music_plan_*.json"):
        pipeline._remove(d, rel)
    if j["status"] in ("done", "error", "paused"):
        db.update(jid, status="queued", error="")
        worker.wake()
    return {"ok": True}


@app.post("/api/jobs/{jid}/revoice")
def job_revoice(jid: str, data: dict = Body(...)):
    """Another voice (or speed and tone) for a voiced video: the voice is made again and the video reassembled; the
    script, scenes, pictures and animations stay."""
    j = _job_or_404(jid)
    if j["status"] == "running":
        raise UserError("Подождите, пока ролик доделается, или поставьте его на паузу.")
    d = pipeline.job_dir(jid)
    if not (d / "voice.json").exists():
        raise UserError("Озвучки у этого ролика ещё нет: голос выбирается в проекте или на странице нового ролика.")
    opts = dict(j["options"])
    before = dict(opts)
    if data.get("voice_id"):
        opts.update(voice_id=str(data["voice_id"]), voice_name=str(data.get("voice_name") or ""))
    for k, lo, hi in (("voice_speed", 0.7, 1.2), ("voice_stability", 0, 1), ("voice_similarity", 0, 1), ("voice_style", 0, 1),
                      ("voice_tempo", pipeline.TEMPO_MIN, pipeline.TEMPO_MAX)):
        if data.get(k) not in (None, ""):
            try:
                opts[k] = min(hi, max(lo, float(data[k])))
            except (TypeError, ValueError):
                raise UserError("Проверьте числа в настройках голоса.")
    db.update(jid, options=opts)
    changed = {k for k in set(opts) | set(before) if opts.get(k) != before.get(k)}
    if changed <= {"voice_tempo"}:
        pipeline.retempo(jid)  # only the tempo: the clips ElevenLabs made are reused, nothing is paid
    else:
        pipeline.reset_from(jid, "voice")
    if j["status"] in ("done", "error", "paused", "review"):
        db.update(jid, status="queued", error="")
        worker.wake()
    return {"ok": True}


@app.post("/api/jobs/{jid}/ideas/more")
def job_more_ideas(jid: str):
    j = _job_or_404(jid)
    if j["options"].get("montage"):
        raise UserError("Это монтаж по вашему тексту: темы здесь не придумываются.")
    if j["status"] == "running":
        raise UserError("Подождите, пока ролик доделается, или поставьте его на паузу.")
    return {"added": pipeline.more_ideas(jid)}


@app.post("/api/jobs/{jid}/translate")
def job_translate(jid: str, data: dict = Body(...)):
    """A version of a finished video in another language: the same pictures and animation, a new voice and text."""
    j = _job_or_404(jid)
    if j["status"] == "running":
        raise UserError("Подождите, пока ролик доделается.")
    lang = data.get("language") or "en"
    if lang not in projects.LANGUAGES or lang == pipeline.job_language(j):
        raise UserError("Выберите язык, на который переводить.")
    voice_id = (data.get("voice_id") or "").strip()
    if not voice_id:
        raise UserError("Выберите голос для новой версии.")
    channel_id = (data.get("channel_id") or "").strip()
    new = pipeline.make_translation(jid, lang, voice_id, (data.get("voice_name") or "").strip(), channel_id,
                                    bool(data.get("restyle", True)), int(data.get("redraw") or 0))
    pid = j["options"].get("project_id")
    if pid and projects.get(pid) and lang != "ru":  # the next version in this language starts with the same choice
        projects.save(pid, {f"{lang}_voice_id": voice_id, f"{lang}_voice_name": (data.get("voice_name") or "").strip(),
                            f"{lang}_channel_id": channel_id, "tr_lang": lang})
    worker.wake()
    return new


@app.post("/api/jobs/{jid}/golden")
def job_golden(jid: str, data: dict = Body(...)):
    """Puts this video's script into its project's library of successful scripts."""
    j = _job_or_404(jid)
    pid = j["options"].get("project_id")
    if not pid or not projects.get(pid):
        raise UserError("Ролик сделан без проекта. Копилка удачных сценариев ведётся для каждого проекта отдельно.")
    d = pipeline.job_dir(jid)
    kind = data.get("kind", "video")
    if kind == "short":
        s = pipeline.read_json(d / "short_script.json") or {}
        title, text, why = s.get("title", ""), s.get("script", ""), s.get("why_ru", "")
        title_ru = s.get("title_ru", "")
    else:
        rv = pipeline.read_json(d / "review.json") or {}
        title, text = j.get("title", ""), rv.get("final_script", "")
        title_ru = j["options"].get("title_ru", "")
        idea = pipeline.read_json(d / "idea.json") or {}
        try:
            why = idea["ideas"][idea["chosen_index"]].get("premise_ru", "")
        except (KeyError, IndexError, TypeError):
            why = ""
        if j["options"].get("format") == "short":
            kind = "short"
    if not text:
        raise UserError("У ролика ещё нет готового текста.")
    stats = (pipeline.read_json(d / "stats.json", {}) or {}).get("short" if kind == "short" and j["options"].get("format") != "short" else "video") or {}
    entry = projects.add_golden(pid, {"job": jid, "kind": kind, "title": title, "title_ru": title_ru,
                                      "text": text, "why_ru": why, "note_ru": (data.get("note") or "").strip()[:500],
                                      "views": stats.get("views")})
    return {"ok": True, "id": entry["id"]}


@app.get("/api/projects/{pid}/golden")
def project_golden(pid: str):
    """The library plus hits from the YouTube numbers that are not in it yet (views well above the usual)."""
    items = projects.list_golden(pid)
    have = {(x.get("job"), x.get("kind")) for x in items}
    suggest = []
    try:
        vids = analytics.project_videos(pid)
    except Exception:
        vids = []
    for kind in ("long", "shorts"):
        group = [v for v in vids if v["kind"] == kind and v.get("views") is not None]
        if len(group) < 3:
            continue
        views = sorted(v["views"] for v in group)
        median = views[len(views) // 2]
        for v in sorted(group, key=lambda v: v["views"], reverse=True)[:5]:
            gk = "short" if kind == "shorts" else "video"
            src = "short" if kind == "shorts" and (db.get(v["job_id"]) or {}).get("options", {}).get("format") != "short" else "video"
            if v["views"] >= max(median * 1.5, 1) and (v["job_id"], gk) not in have:
                suggest.append({"job": v["job_id"], "kind": src, "title": v["title"], "views": v["views"],
                                "avg_view_pct": v.get("avg_view_pct"), "median": median})
    return {"items": items, "suggest": suggest}


@app.get("/api/projects/{pid}/places")
def project_places(pid: str):
    return {"items": [{"name": x["name"], "h": bool(x.get("h")), "v": bool(x.get("v"))} for x in projects.list_places(pid)]}


@app.delete("/api/projects/{pid}/places")
def project_places_clear(pid: str):
    projects.clear_places(pid)
    return {"ok": True}


@app.delete("/api/projects/{pid}/golden/{gid}")
def project_golden_delete(pid: str, gid: str):
    projects.remove_golden(pid, gid)
    return {"ok": True}


@app.post("/api/jobs/{jid}/speed")
def job_speed(jid: str, data: dict = Body(...)):
    """Speeds up a finished video or Short (or puts the original back). Free: FFmpeg on this computer."""
    j = _job_or_404(jid)
    kind = data.get("kind", "video")
    if kind not in ("video", "short"):
        raise HTTPException(400, "Неизвестный тип ролика")
    if j["status"] == "running":
        raise UserError("Подождите, пока ролик доделается, или поставьте его на паузу.")
    try:
        speed = float(data.get("speed") or 1)
    except ValueError:
        raise UserError("Скорость должна быть числом, например 1.2")
    return pipeline.set_speed(jid, kind, speed)


@app.post("/api/jobs/{jid}/upload")
def job_upload(jid: str, data: dict = Body(...)):
    _job_or_404(jid)
    uploader.start(jid, data.get("kind", "video"), data.get("channel_id", ""), data.get("privacy") or None,
                   (data.get("publish_at") or "").strip())
    return {"ok": True}


@app.post("/api/jobs/{jid}/instagram")
def job_instagram(jid: str):
    _job_or_404(jid)
    uploader.start_instagram(jid)
    return {"ok": True}


@app.get("/api/update/check")
def update_check():
    return updater.check()


@app.post("/api/update/apply")
def update_apply():
    updater.start()
    return {"ok": True}


@app.get("/api/update/status")
def update_status():
    return updater.state


@app.get("/api/balance")
def balance_all():
    return {"services": balance.check_all(), "low_usd": balance.LOW_USD}


@app.post("/api/balance/known")
def balance_known(data: dict = Body(...)):
    try:
        amount = float(str(data.get("amount", "")).replace(",", ".").replace("$", "").strip())
    except ValueError:
        raise UserError("Впишите сумму числом, например 12.50")
    balance.set_known(data.get("service", ""), amount)
    return {"ok": True}


@app.post("/api/instagram/connect")
def instagram_connect(data: dict = Body(...)):
    # The app secret is used once for the exchange and never stored.
    return instagram.connect(data.get("app_id", ""), data.get("app_secret", ""), data.get("token", ""))


@app.post("/api/instagram/disconnect")
def instagram_disconnect():
    from .config import clear_settings
    clear_settings("ig_token", "ig_user_token", "ig_user_id", "ig_username", "ig_page", "ig_login")
    return {"ok": True}


@app.post("/api/jobs/{jid}/youtube_thumb")
def job_youtube_thumb(jid: str, data: dict = Body(...)):
    _job_or_404(jid)
    uploader.send_thumbnail(jid, data.get("kind", "video"))
    return {"ok": True}


@app.post("/api/jobs/{jid}/link")
def job_link(jid: str, data: dict = Body(...)):
    """A video the owner uploaded himself: its link lets the program collect its numbers."""
    _job_or_404(jid)
    kind = data.get("kind", "video")
    if kind not in ("video", "short"):
        raise HTTPException(400, "Неизвестный тип ролика")
    analytics.link_manual(jid, kind, data.get("link", ""))
    return {"ok": True}


@app.get("/api/projects/{pid}/stats")
def project_stats(pid: str):
    vids = analytics.project_videos(pid)
    return {"videos": vids, "lessons": analytics.get_lessons(pid), "ready": analytics.ready(pid),
            "min_age_days": analytics.MIN_AGE_DAYS, "last_run": analytics.status["last_run"],
            "error": analytics.status["error"], "channels": len(youtube.list_channels())}


@app.post("/api/stats/refresh")
def stats_refresh():
    return analytics.collect()


@app.post("/api/projects/{pid}/lessons")
def project_learn(pid: str):
    return analytics.learn(pid)


@app.post("/api/projects/{pid}/lessons/{rid}")
def project_rule(pid: str, rid: str, data: dict = Body(...)):
    analytics.set_rule(pid, rid, off=data.get("off"), delete=bool(data.get("delete")))
    return analytics.get_lessons(pid)


# ---------------------------------------------------------------- projects

@app.get("/api/projects")
def projects_list():
    counts = {}
    for j in db.list_all():
        pid = j["options"].get("project_id", "")
        counts[pid] = counts.get(pid, 0) + 1
    return [{**p, "count": counts.get(p["id"], 0)} for p in projects.list_all()]


@app.post("/api/projects")
def project_create(data: dict = Body(...)):
    preset = data.get("preset", "")
    ig = data.get("platform") == "instagram"
    if preset == "custom":
        s = load_settings()
        return projects.save("", {"name": "Новый проект", "icon": "📸" if ig else "🎬", "niche": "",
                                  "duration_min": s["duration_min"], **(projects.INSTAGRAM if ig else {})})
    platform = "instagram" if ig else "youtube"
    have = projects.project_of(preset, platform)  # one project per niche: the existing one opens instead of a copy
    return projects.get(have) if have else projects.create_from_preset(preset, load_settings(), platform)


@app.get("/api/niches")
def niches():
    hidden = set(load_settings().get("hidden_niches") or [])
    return {k: {"name": v["name"], "icon": v.get("icon", "🎬"), "language": v.get("language", "en"),
                "mine": bool(v.get("mine")), "hidden": k in hidden, "project": projects.project_of(k)}
            for k, v in projects.all_presets().items()}


@app.post("/api/niches/{key}/hidden")
def niche_hide(key: str, data: dict = Body(...)):
    """Ready niches can't be deleted, only hidden from the list (and shown again later)."""
    hidden = [k for k in (load_settings().get("hidden_niches") or []) if k != key]
    if data.get("hidden"):
        hidden.append(key)
    save_settings({"hidden_niches": hidden})
    return {"ok": True}


@app.post("/api/projects/{pid}/template")
def project_template(pid: str):
    return {"key": projects.save_template(pid)}


@app.delete("/api/niches/{key}")
def niche_delete(key: str):
    projects.remove_template(key)
    return {"ok": True}


@app.post("/api/projects/{pid}")
def project_save(pid: str, data: dict = Body(...)):
    return projects.save(pid, data)


@app.delete("/api/projects/{pid}")
def project_delete(pid: str):
    projects.remove(pid)
    projects.clear_places(pid)
    return {"ok": True}


# ---------------------------------------------------------------- characters

CHAR_SCHEMA = {"type": "object", "properties": {
    "name_en": {"type": "string"}, "appearance": {"type": "string"}, "personality": {"type": "string"}}}


def _char_photos(cid: str) -> list:
    """The character's photos as small JPEGs for the model to look at."""
    from PIL import Image
    import io
    out = []
    main = projects.portrait_path(cid)
    for f in ([main] if main.exists() else []) + projects.list_refs(cid):
        im = Image.open(f).convert("RGB")
        im.thumbnail((1024, 1024))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
        out.append(buf.getvalue())
    return out[:9]


def _describe_char(c: dict, proj: dict) -> dict:
    """An exact English visual description for the image model: from the owner's photos when there are any."""
    s = load_settings()
    photos = _char_photos(c["id"]) if c.get("id") else []
    system = ("You help a faceless YouTube channel keep a recurring character consistent across hundreds of "
              "AI-generated images and scripts. Answer only with JSON.")
    if photos:
        look = ("The attached image(s) show this character (possibly from several sides). Describe ONLY what you "
                "actually see in the images: do not invent or change clothing, shoes, accessories or body details. "
                "If something is not visible (for example the legs), do not describe it at all. "
                "Use the owner's text only for things the images cannot show (name, age, character).")
    else:
        look = "There are no photos: build the look from the owner's description."
    user = (f"Channel niche: {proj.get('niche', '')}\nCharacter name: {c['name']}\nRole in the videos: {c.get('role', '')}\n"
            f"Description from the owner (may be in Russian): {c.get('description_ru', '')}\n\n{look}\n\n"
            "Return:\n- name_en: the name as it should be written in English narration (transliterate if needed);\n"
            "- appearance: 40-80 words, an exact, reusable visual description in English: age, gender, face, hair, "
            "eyes, build, clothing and colors, shoes, signature details (front and back if shown). No art style words;\n"
            "- personality: 1-2 sentences in English about character, voice and behaviour, for the scriptwriter.")
    return llm.call_json(s, system, user, CHAR_SCHEMA, {}, 2000, images=photos)


@app.get("/api/projects/{pid}/episodes")
def episodes_list(pid: str):
    proj = projects.get(pid)
    return {"episodes": projects.list_episodes(pid), "next": _next_episode(proj) if proj else 1}


@app.delete("/api/projects/{pid}/episodes/{num}")
def episode_delete(pid: str, num: int):
    projects.remove_episode(pid, num)
    return {"ok": True}


SERIES_SCHEMA = {"type": "object", "properties": {
    "series_bible": {"type": "string"},
    "episodes": {"type": "array", "items": {"type": "string"}}},
    "required": ["series_bible", "episodes"]}


@app.post("/api/projects/{pid}/series/plan")
def series_plan(pid: str, data: dict = Body(...)):
    """Drafts a series bible and a season plan in Russian; the owner edits them before saving."""
    proj = {**projects.get(pid), **{k: v for k, v in data.items() if k in ("niche", "series_bible", "season_plan")}}
    if not proj.get("id"):
        raise HTTPException(404, "Проект не найден")
    chars = projects.list_chars(set(data.get("characters") or proj.get("characters") or []))
    n = max(3, min(int(data.get("count") or 10), 30))
    char_text = "\n".join(f"- {c['name']} ({c.get('role', '')}): {c.get('description_ru', '')} {c.get('personality', '')}"
                          + (f" Manner of speech: {c['speech_ru']}" if c.get("speech_ru") else "") for c in chars) or "(no characters yet: invent 1-2 main characters and describe them)"
    done = projects.list_episodes(pid)
    done_text = "\n".join(f"- Episode {e['num']}: {e.get('summary_ru') or e.get('summary', '')}" for e in done)
    system = ("You are the showrunner of a faceless YouTube animated series. You plan seasons that keep viewers "
              "coming back: a strong premise, clear rules of the world, characters who change, and an arc where "
              "each episode is a complete story that also moves the big story forward. Answer only with JSON.")
    user = (f"Channel niche: {proj.get('niche', '')}\nLanguage of the videos: "
            f"{'Russian' if proj.get('language') == 'ru' else 'English'}\n"
            f"{'Videos for young children: keep everything kind and safe.' if proj.get('made_for_kids') else ''}\n"
            f"Main characters:\n{char_text}\n\n"
            f"Current bible from the owner (keep what is there and improve it):\n{proj.get('series_bible') or '(empty)'}\n\n"
            + (f"Episodes already released (continue after them):\n{done_text}\n\n" if done else "")
            + "Write everything in natural Russian (the owner does not speak English).\n"
            "- series_bible: 150-300 words, with short labelled paragraphs: Premise; World and its rules; Main "
            "characters and how each one changes over the season; The big season question; Tone; Recurring things "
            "(a catchphrase, a running gag, a ritual opening or ending).\n"
            f"- episodes: exactly {n} lines, one per episode"
            + (f", starting from episode {len(done) + 1}" if done else "")
            + ": a title and one or two sentences of plot. The season builds to a climax in the last episode; "
            "the middle has a turning point.")
    try:
        r = llm.call_json(load_settings(), system, user, SERIES_SCHEMA, {}, 6000)
    except UserError as e:
        raise HTTPException(400, str(e))
    start = len(done) + 1
    lines = [f"{start + i}. " + re.sub(r"^\s*\d+\s*[.)]\s*", "", t).strip() for i, t in enumerate(r.get("episodes") or [])]
    return {"series_bible": r.get("series_bible", "").strip(), "season_plan": "\n".join(lines)}


@app.get("/api/characters")
def chars_list():
    return projects.list_chars()


@app.post("/api/characters")
def char_create(data: dict = Body(...)):
    return _char_save("", data)


@app.post("/api/characters/{cid}")
def char_update(cid: str, data: dict = Body(...)):
    return _char_save(cid, data)


def _char_save(cid, data):
    c = projects.save_char(cid, data)
    if data.get("later"):
        return c  # the page uploads the photos first and then asks for the description
    if data.get("describe") or not c.get("appearance"):
        try:
            c = projects.save_char(c["id"], {**c, **_describe_char(c, projects.get(c.get("style_project", "")))})
        except UserError as e:
            return {**c, "warning": f"Персонаж сохранён, но описание внешности для картинок не получилось: {e} "
                                    "Можно вписать внешность по-английски вручную в «Изменить»."}
    return c


@app.delete("/api/characters/{cid}")
def char_delete(cid: str):
    projects.remove_char(cid)
    return {"ok": True}


@app.post("/api/characters/{cid}/portrait")
def char_portrait(cid: str):
    c = projects.get_char(cid)
    if not c:
        raise UserError("Персонаж не найден")
    s = load_settings()
    style = c.get("style") or projects.get(c.get("style_project", "")).get("image_style") or s.get("image_style", "")
    prompt = (f"Character portrait, head and shoulders, looking at the viewer, neutral simple background, "
              f"even soft light. {c.get('appearance') or c.get('description_ru')}. {style}")
    photos = ([projects.portrait_path(cid)] if projects.portrait_path(cid).exists() else []) + projects.list_refs(cid)
    if photos and s.get("fal_key"):
        # Draw the portrait in the chosen style but from the owner's photos, so the face stays the same.
        raw = images.generate_with_reference(s, prompt, [photos], False, square=True)
    else:
        raw = images.generate(s, prompt, False)
    media.to_png(raw, projects.portrait_path(cid))
    return {"ok": True}


@app.post("/api/characters/{cid}/upload")
async def char_upload(cid: str, request: Request):
    if not projects.get_char(cid):
        raise UserError("Персонаж не найден")
    raw = await request.body()
    if len(raw) < 100 or len(raw) > 30_000_000:
        raise UserError("Не похоже на картинку. Подойдёт JPG или PNG.")
    media.to_png(raw, projects.portrait_path(cid))
    return {"ok": True}


@app.post("/api/characters/{cid}/refs")
async def char_ref_add(cid: str, request: Request):
    if not projects.get_char(cid):
        raise UserError("Персонаж не найден")
    raw = await request.body()
    if len(raw) < 100 or len(raw) > 30_000_000:
        raise UserError("Не похоже на картинку. Подойдёт JPG или PNG.")
    media.to_png(raw, projects.new_ref_path(cid))
    return {"ok": True}


@app.delete("/api/characters/{cid}/refs/{name}")
def char_ref_delete(cid: str, name: str):
    projects.ref_path(cid, name).unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/characters/{cid}/refs/{name}/main")
def char_ref_main(cid: str, name: str):
    projects.make_main(cid, name)
    return {"ok": True}


@app.get("/charrefs/{cid}/{name}")
def char_ref_file(cid: str, name: str):
    p = projects.ref_path(cid, name)
    if not projects.get_char(cid) or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


@app.get("/portraits/{cid}.png")
def char_portrait_file(cid: str):
    if not projects.get_char(cid):
        raise HTTPException(404)
    p = projects.portrait_path(cid)
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------- items

ITEM_SCHEMA = {"type": "object", "properties": {"name_en": {"type": "string"}, "appearance": {"type": "string"}}}


def _item_photos(iid: str) -> list:
    from PIL import Image
    import io
    out = []
    main = projects.item_photo_path(iid)
    for f in ([main] if main.exists() else []) + projects.list_item_refs(iid):
        im = Image.open(f).convert("RGB")
        im.thumbnail((1024, 1024))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
        out.append(buf.getvalue())
    return out[:9]


def _describe_item(it: dict) -> dict:
    """An exact English description of the product for the image model, strictly from the owner's photos."""
    photos = _item_photos(it["id"])
    system = ("You help a faceless YouTube channel show the same real product or object in many AI-generated "
              "images, so it must always look exactly like the original. Answer only with JSON.")
    if photos:
        look = ("The attached image(s) show this item (possibly from several sides). Describe ONLY what you actually "
                "see: do not invent or change anything. If a side is not visible, do not describe it.")
    else:
        look = "There are no photos: build the look from the owner's description."
    user = (f"Item name: {it['name']}\nKind: {it.get('kind', '')}\n"
            f"Description from the owner (may be in Russian): {it.get('description_ru', '')}\n\n{look}\n\n"
            "Return:\n- name_en: the name as it should be written in English narration (keep brand names as they are);\n"
            "- appearance: 40-90 words, an exact, reusable visual description in English: what the object is, shape and "
            "proportions, size relative to a hand, colours, materials and finish, parts and buttons, where the logo, "
            "labels and text are placed and what they say, packaging if shown. No art style words.")
    return llm.call_json(load_settings(), system, user, ITEM_SCHEMA, {}, 2000, images=photos)


@app.get("/api/items")
def items_list():
    return projects.list_items()


@app.post("/api/items")
def item_create(data: dict = Body(...)):
    return _item_save("", data)


@app.post("/api/items/{iid}")
def item_update(iid: str, data: dict = Body(...)):
    return _item_save(iid, data)


def _item_save(iid, data):
    it = projects.save_item(iid, data)
    if data.get("later"):
        return it  # the page uploads the photos first and then asks for the description
    if data.get("describe") or not it.get("appearance"):
        try:
            it = projects.save_item(it["id"], {**it, **_describe_item(it)})
        except UserError as e:
            return {**it, "warning": f"Предмет сохранён, но описание для картинок не получилось: {e} "
                                     "Можно вписать описание по-английски вручную в «Изменить»."}
    return it


@app.delete("/api/items/{iid}")
def item_delete(iid: str):
    projects.remove_item(iid)
    return {"ok": True}


async def _item_image(iid: str, request: Request) -> bytes:
    if not projects.get_item(iid):
        raise UserError("Предмет не найден")
    raw = await request.body()
    if len(raw) < 100 or len(raw) > 30_000_000:
        raise UserError("Не похоже на картинку. Подойдёт JPG или PNG.")
    return raw


@app.post("/api/items/{iid}/upload")
async def item_upload(iid: str, request: Request):
    media.to_png(await _item_image(iid, request), projects.item_photo_path(iid))
    return {"ok": True}


@app.post("/api/items/{iid}/refs")
async def item_ref_add(iid: str, request: Request):
    raw = await _item_image(iid, request)
    media.to_png(raw, projects.new_item_ref_path(iid))
    return {"ok": True}


@app.delete("/api/items/{iid}/refs/{name}")
def item_ref_delete(iid: str, name: str):
    projects.item_ref_path(iid, name).unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/items/{iid}/refs/{name}/main")
def item_ref_main(iid: str, name: str):
    projects.item_make_main(iid, name)
    return {"ok": True}


@app.get("/itemrefs/{iid}/{name}")
def item_ref_file(iid: str, name: str):
    p = projects.item_ref_path(iid, name)
    if not projects.get_item(iid) or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


@app.get("/itemphotos/{iid}.png")
def item_photo_file(iid: str):
    if not projects.get_item(iid):
        raise HTTPException(404)
    p = projects.item_photo_path(iid)
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------- YouTube channels

@app.get("/api/youtube")
def yt_status():
    return {"has_client": youtube.has_client(), "channels": youtube.list_channels(),
            "categories": youtube.CATEGORIES, "last_error": youtube.last_error}


@app.post("/api/youtube/client")
async def yt_client(request: Request):
    youtube.save_client(await request.body())
    return {"ok": True}


@app.get("/api/youtube/login")
def yt_login(request: Request):
    youtube.last_error = ""
    return RedirectResponse(youtube.auth_url(str(request.base_url)))


@app.post("/api/youtube/channels/{cid}")
def yt_profile(cid: str, data: dict = Body(...)):
    youtube.save_profile(cid, data)
    return {"ok": True}


@app.delete("/api/youtube/channels/{cid}")
def yt_remove(cid: str):
    youtube.remove(cid)
    return {"ok": True}


@app.get("/files/{jid}/{path:path}")
def job_file(jid: str, path: str):
    base = pipeline.job_dir(jid).resolve()
    p = (base / path).resolve()
    if base not in p.parents or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


def _open(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    cmd = "open" if sys.platform == "darwin" else ("explorer" if sys.platform.startswith("win") else "xdg-open")
    try:
        subprocess.Popen([cmd, str(path)])
    except OSError:
        raise UserError(f"Не получилось открыть папку. Она находится здесь: {path}")


# ---------------------------------------------------------------- remake of someone else's video

@app.get("/api/remakes")
def remakes_list():
    return {"items": remake.list_all(), "subs_modes": remake.SUBS_MODES,
            "replicate": bool(load_settings().get("replicate_key"))}


@app.post("/api/remakes")
async def remakes_create(request: Request, name: str = ""):
    raw = await request.body()
    if len(raw) < 1000:
        raise UserError("Файл пустой. Выберите видео mp4.")
    return remake.create(Path(name).name, raw)


@app.get("/api/remakes/{rid}")
def remakes_get(rid: str):
    return remake.get(rid)


@app.post("/api/remakes/{rid}/start")
def remakes_start(rid: str, data: dict = Body(...)):
    remake.start(rid, data)
    return remake.get(rid)


@app.delete("/api/remakes/{rid}")
def remakes_delete(rid: str):
    remake.delete(rid)
    return {"ok": True}


@app.get("/api/remakes/{rid}/file/{name}")
def remakes_file(rid: str, name: str):
    d = remake._dir(rid)
    p = d / name if name in ("preview.jpg", "source.mp4") else d / "output" / Path(name).name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------- AI producer and the niche script library

@app.get("/api/producer")
def producer_list():
    return {"items": producer.list_all(), "downloader": producer._ytdlp_version(), "browsers": producer.BROWSERS,
            "installed": producer._installed_browsers(), "browser": load_settings().get("yt_cookies_browser", "")}


@app.post("/api/producer")
async def producer_start(request: Request, name: str = ""):
    if name:  # an uploaded video
        raw = await request.body()
        if len(raw) < 1000:
            raise UserError("Файл пустой. Выберите видео mp4.")
        return producer.start(file_bytes=raw, file_name=Path(name).name, platform=request.query_params.get("platform", ""))
    data = await request.json()
    return producer.start(data.get("link", ""), data.get("count", 5), data.get("text", ""),
                          mode=data.get("mode", ""), tab=data.get("tab", "auto"), platform=data.get("platform", ""))


@app.post("/api/producer/update-downloader")
def producer_update():
    return {"version": producer.update_downloader()}


@app.get("/api/producer/{aid}")
def producer_get(aid: str):
    return producer.get(aid)


def _forget(pid: str, gone: list) -> str:
    """Rules and niche description without what came from removed analyses. Returns a note for the owner."""
    if not gone:
        return ""
    proj, lib = projects.get(pid), projects.library(pid)
    if not (lib.get("playbook_ru") or "").strip():  # no rules were built from it: nothing else to clean
        return ""
    try:
        r = producer.forget_playbook(load_settings(), proj.get("niche", ""), lib.get("playbook_ru", ""), lib["items"], gone)
    except UserError as e:
        return f"Разбор убран, но правила ниши пересобрать не получилось ({e}). Нажмите «Обновить правила» позже."
    niche = (r.get("niche") or "").strip()
    old = proj.get("niche", "")
    if niche and niche != old.strip():
        projects.save(pid, {"niche": niche})
    projects.set_playbook(pid, r.get("playbook_ru", ""), r.get("playbook", ""),
                          niche_prev=old if niche and niche != old.strip() else None)
    return r.get("changed_ru") or "Правила ниши пересобраны без этого разбора."


@app.delete("/api/producer/{aid}")
def producer_delete(aid: str, library: bool = False):
    notes = []
    if library:  # also take everything this analysis saved out of every niche library
        sources = producer.library_sources(aid)
        for p in projects.list_all():
            gone = projects.remove_library(p["id"], sources=sources)
            if gone:
                note = _forget(p["id"], gone)
                notes.append(f"{p.get('name', '')}: убрано {len(gone)}. {note}".strip())
    producer.delete(aid)
    return {"ok": True, "notes": notes}


@app.post("/api/producer/{aid}/save")
def producer_save(aid: str, data: dict = Body(...)):
    pid = data.get("project_id", "")
    n = projects.add_library(pid, producer.library_entries(aid, data.get("indexes"), bool(data.get("with_channel", True))))
    return {"added": n}


@app.get("/api/projects/{pid}/library")
def library_get(pid: str):
    if not projects.get(pid):
        raise UserError("Проект не найден")
    return projects.library(pid)


@app.post("/api/projects/{pid}/library/remove")
def library_remove_many(pid: str, data: dict = Body(...)):
    """Several entries at once, with one rebuild of the rules."""
    ids = {str(x) for x in data.get("ids") or []}
    if not ids:
        raise UserError("Отметьте разборы, которые убрать.")
    gone = projects.remove_library(pid, ids=ids)
    note = _forget(pid, gone)
    return {**projects.library(pid), "note": f"Убрано разборов: {len(gone)}. {note}".strip(), "niche": projects.get(pid).get("niche", "")}


@app.delete("/api/projects/{pid}/library/{eid}")
def library_remove(pid: str, eid: str):
    note = _forget(pid, projects.remove_library(pid, eid))
    return {**projects.library(pid), "note": note, "niche": projects.get(pid).get("niche", "")}


@app.post("/api/projects/{pid}/library/playbook")
def library_playbook(pid: str, data: dict = Body(...)):
    return projects.set_playbook(pid, data.get("playbook_ru", ""))


@app.post("/api/projects/{pid}/library/niche-undo")
def library_niche_undo(pid: str):
    prev = projects.library(pid).get("niche_prev")
    if prev is None:
        raise UserError("Прежнего описания нет.")
    projects.save(pid, {"niche": prev})
    return {"niche": prev}


@app.post("/api/projects/{pid}/library/build")
def library_build(pid: str):
    proj = projects.get(pid)
    if not proj:
        raise UserError("Проект не найден")
    own = "" if projects.is_placeholder(proj.get("niche", "")) else proj.get("niche", "")  # a leftover default is replaced
    pb = producer.build_playbook(load_settings(), own, projects.library(pid)["items"])
    if (pb.get("niche") or "").strip():
        projects.save(pid, {"niche": pb["niche"].strip()})
    lib = projects.set_playbook(pid, pb.get("playbook_ru", ""), pb.get("playbook", ""), niche_prev=proj.get("niche", ""))
    return lib


@app.post("/api/open")
def open_folder(data: dict = Body(...)):
    what = data.get("what")
    if what == "job":
        f = exporter.export_job(data["id"])  # the video's own folder in Movies; the program's copy if nothing is ready
        _open(f or pipeline.job_dir(data["id"]) / "output")
    elif what == "remake":
        m = remake.get(data["id"])
        _open(exporter.export_remake(data["id"], m, remake._dir(data["id"]) / "output") or remake._dir(data["id"]) / "output")
    elif what == "videos":
        exporter.ROOT.mkdir(parents=True, exist_ok=True)
        _open(exporter.ROOT)
    elif what == "music":
        _open(MUSIC_DIR)
    elif what == "prompts":
        _open(PROMPTS_DIR)
    else:
        _open(DATA_DIR)
    return {"ok": True}


# ---------------------------------------------------------------- prompts

PROMPT_TITLES = {
    "00_system.txt": "Общие правила для нейросети (ниша, стиль, язык)",
    "01_ideas.txt": "Шаг 1. Придумать темы (по умолчанию 5) и выбрать лучшую",
    "02_outline.txt": "Шаг 2. Структура: хук, повороты, удерживающие крючки",
    "03_script.txt": "Шаг 3. Написать текст",
    "04_review.txt": "Шаг 4. Редактор: чек-лист и переписывание",
    "05_scenes.txt": "Шаг 5. Описания картинок для сцен",
    "06_extras.txt": "Шаг 6. Перевод, название, описание, теги, обложка",
    "07_short.txt": "Шаг 7. Сценарий шортса",
}


@app.get("/api/prompts")
def prompts():
    out = []
    for f in sorted(PROMPTS_DEFAULT_DIR.glob("*.txt")):
        user = PROMPTS_DIR / f.name
        text = user.read_text("utf-8") if user.exists() else f.read_text("utf-8")
        out.append({"name": f.name, "title": PROMPT_TITLES.get(f.name, f.name), "text": text,
                    "modified": text != f.read_text("utf-8")})
    return out


@app.post("/api/prompts/{name}")
def save_prompt(name: str, data: dict = Body(...)):
    default = PROMPTS_DEFAULT_DIR / name
    if not default.exists():
        raise HTTPException(404)
    target = PROMPTS_DIR / name
    text = data.get("text", "")
    # Only edited templates are kept; an unchanged one follows the program's improvements.
    if data.get("reset") or text.strip() == default.read_text("utf-8").strip():
        target.unlink(missing_ok=True)
        return {"ok": True, "text": default.read_text("utf-8")}
    target.write_text(text, "utf-8")
    return {"ok": True, "text": text}


# ---------------------------------------------------------------- music

MUSIC_EXT = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")


@app.get("/api/music")
def music():
    styles = musiclib.styles()
    return [{"name": f.name, "size": f.stat().st_size, "style": styles.get(f.name, {}).get("style_ru", ""),
             "made_for": styles.get(f.name, {}).get("made_for", "")} for f in musiclib.tracks()]


@app.post("/api/music/{name}/style")
def music_style(name: str, data: dict = Body(...)):
    musiclib.set_style(Path(name).name, str(data.get("style") or ""))
    return {"ok": True}


@app.post("/api/music/upload")
async def music_upload(request: Request, name: str):
    safe = Path(name).name
    if Path(safe).suffix.lower() not in MUSIC_EXT:
        raise UserError("Подходят только звуковые файлы: mp3, wav, m4a, aac, ogg, flac")
    (MUSIC_DIR / safe).write_bytes(await request.body())
    return {"ok": True}


@app.delete("/api/music/{name}")
def music_delete(name: str):
    (MUSIC_DIR / Path(name).name).unlink(missing_ok=True)
    musiclib.forget(Path(name).name)
    return {"ok": True}


@app.get("/music/{name}")
def music_file(name: str):
    p = MUSIC_DIR / Path(name).name
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(p)


# ---------------------------------------------------------------- service checks

@app.post("/api/test/llm")
def test_llm():
    usage = {}
    r = llm.call_json(load_settings(), "Answer in JSON.",
                      "Reply with a short friendly greeting in Russian in the field 'text'.",
                      {"type": "object", "properties": {"text": {"type": "string"}}}, usage, 2000)
    return {"text": r.get("text", ""), "cost": usage.get("llm_cost", 0)}


@app.get("/api/voices")
def voices():
    return tts.list_voices(load_settings())


@app.get("/api/voices/library")
def voices_library(gender: str = "", use_case: str = "", search: str = "", page: int = 0, language: str = "ru"):
    return tts.library(load_settings(), gender, use_case, search, page, language)


@app.post("/api/voices/library/add")
def voices_library_add(data: dict = Body(...)):
    s = load_settings()
    vid = tts.add_from_library(s, data.get("owner", ""), data.get("voice_id", ""), data.get("name", ""))
    # Library voices found through a language filter are native speakers of that language.
    lang = data.get("language") if data.get("language") in tts.AUDIENCES[:5] else "ru"
    save_settings({"voice_audience": {**(s.get("voice_audience") or {}), vid: lang}})
    return {"voice_id": vid, "name": data.get("name", "")}


@app.post("/api/voices/{vid}/audience")
def voice_audience(vid: str, data: dict = Body(...)):
    """The owner marks who a voice suits: ru, en, de, es, fr or both ('auto' forgets the mark)."""
    s = load_settings()
    marks = dict(s.get("voice_audience") or {})
    if data.get("audience") in tts.AUDIENCES:
        marks[vid[:64]] = data["audience"]
    else:
        marks.pop(vid, None)
    save_settings({"voice_audience": marks})
    return {"ok": True}


@app.get("/api/elevenlabs/account")
def el_account():
    return tts.account_info(load_settings())


SAMPLE_TEXT = {
    "ru": ("Зимой тысяча восемьсот семьдесят второго года в Атлантике нашли корабль. "
           "Паруса подняты, груз на месте. Но на борту не было ни души."),
    "de": ("Im Winter achtzehnhundertzweiundsiebzig fand man im Atlantik ein treibendes Schiff. "
           "Die Segel waren gesetzt, die Ladung an Bord. Aber keine einzige Seele."),
    "es": ("En el invierno de mil ochocientos setenta y dos, encontraron un barco a la deriva en el Atlántico. "
           "Las velas estaban izadas. Pero a bordo no había ni un alma."),
    "fr": ("À l'hiver mille huit cent soixante-douze, on retrouva un navire à la dérive dans l'Atlantique. "
           "Les voiles étaient hissées. Mais à bord, il n'y avait plus personne."),
}


@app.post("/api/test/tts")
def test_tts(data: dict = Body(default={})):
    s = load_settings()
    text = data.get("text") or ("In the winter of 1872, a ship was found drifting in the Atlantic. "
                                "Her sails were set. But not a single soul was on board.")
    proj = projects.get(data.get("project_id") or "")
    if proj:
        # The same order as in a real video: channel, then project, then the host character.
        if proj.get("channel_id"):
            prof = youtube.get_profile(proj["channel_id"])
            if prof.get("voice_id"):
                s["elevenlabs_voice_id"] = prof["voice_id"]
        if proj.get("voice_id"):
            s["elevenlabs_voice_id"] = proj["voice_id"]
        if proj.get("tts_model"):
            s["elevenlabs_model"] = proj["tts_model"]
        for k in projects.VOICE_NUM:
            if proj.get(k) not in (None, ""):
                s[k] = float(proj[k])
        if proj.get("voice_mode") == "host":
            host = next((c for c in projects.list_chars(set(proj.get("characters") or [])) if c.get("voice_id")), None)
            if host:
                s["elevenlabs_voice_id"] = host["voice_id"]
        if proj.get("language") in SAMPLE_TEXT and not data.get("text"):
            text = SAMPLE_TEXT[proj["language"]]
    out = DATA_DIR / "test_voice.mp3"
    out.with_suffix(".json").unlink(missing_ok=True)
    tts.synthesize(s, text, out, language=proj.get("language", "") if proj else "")
    return Response(out.read_bytes(), media_type="audio/mpeg")


@app.post("/api/test/anim")
def test_anim():
    """Draws one picture and animates it: shows that animation works with your keys (costs ~$0.3-0.7)."""
    s = load_settings()
    img = DATA_DIR / "test_anim.png"
    prompt = "A lighthouse on a rocky cliff at sunset, huge waves crashing, seagulls, clouds moving"
    media.to_png(images.generate(s, f"{prompt}. {s.get('image_style', '')}", False), img)
    data = animate.generate(s, img, prompt, 5.0, False)
    out = DATA_DIR / "test_anim.mp4"
    out.write_bytes(data)
    return Response(data, media_type="video/mp4")


@app.post("/api/test/image")
def test_image(data: dict = Body(default={})):
    s = load_settings()
    if data.get("provider"):
        s["image_provider"] = data["provider"]
    prompt = data.get("prompt") or "An old sailing ship drifting alone in a stormy sea at dawn, empty deck"
    raw = images.generate(s, f"{prompt}. {s.get('image_style', '')}", False)
    out = DATA_DIR / "test_image.png"
    media.to_png(raw, out)
    return Response(out.read_bytes(), media_type="image/png")
