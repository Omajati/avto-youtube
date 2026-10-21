"""Uploads finished videos to YouTube in the background, one at a time.
State per job lives in upload.json: {"video": {...}, "short": {...}}."""
import json
import threading
import time

from . import db, instagram, media, pipeline, projects, thumb, youtube
from .net import UserError

_one_at_a_time = threading.Lock()
_active = set()
_state_lock = threading.Lock()


def _path(jid):
    return pipeline.job_dir(jid) / "upload.json"


def thumb_note(e) -> str:
    return ("Видео загружено, но превью YouTube не принял. Обычно это значит, что канал не подтверждён по телефону "
            "(youtube.com/verify). Подтвердите канал и нажмите «Отправить превью ещё раз», или поставьте превью "
            f"вручную в YouTube Studio. Ответ YouTube: {e}")


def send_thumbnail(jid: str, kind: str = "video"):
    """Sets the program's thumbnail on an already uploaded video."""
    st = state(jid).get(kind) or {}
    if not st.get("video_id"):
        raise UserError("Ролик ещё не загружен на YouTube.")
    if not st.get("channel_id"):
        raise UserError("Этот ролик загружен вручную, программа не может менять его на YouTube. "
                        "Поставьте превью в YouTube Studio: файл thumbnail.jpg лежит в папке ролика, в папке output.")
    thumb = pipeline.job_dir(jid) / "output" / "thumbnail.jpg"
    if not thumb.exists():
        raise UserError("Картинки превью нет. Сделайте её в шаге «Превью».")
    try:
        youtube.set_thumbnail(st["channel_id"], st["video_id"], thumb)
    except UserError as e:
        _set(jid, kind, note=thumb_note(e))
        raise
    _set(jid, kind, note="")
    _log(jid, "YouTube: превью поставлено")


def state(jid: str) -> dict:
    with _state_lock:
        s = pipeline.read_json(_path(jid), {}) or {}
        changed = False
        for kind, st in s.items():
            if st.get("status") in ("queued", "uploading") and (jid, kind) not in _active:
                st.update(status="error", error="Загрузка прервалась (программа была закрыта). Нажмите «"
                          + ("Выложить в Instagram" if kind == "instagram" else "Загрузить") + "» ещё раз.")
                changed = True
        if changed:
            pipeline.write_json(_path(jid), s)
        return s


def _set(jid, kind, **fields):
    with _state_lock:
        s = pipeline.read_json(_path(jid), {}) or {}
        s.setdefault(kind, {}).update(fields)
        pipeline.write_json(_path(jid), s)


def _log(jid, msg):
    try:
        with open(pipeline.job_dir(jid) / "log.txt", "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def start(jid: str, kind: str, channel_id: str, privacy: str = None, publish_at: str = ""):
    if kind not in ("video", "short"):
        raise UserError("Неизвестный тип ролика")
    d = pipeline.job_dir(jid)
    if not (d / "output" / ("video.mp4" if kind == "video" else "short.mp4")).exists():
        raise UserError("Видео ещё не готово.")
    if (jid, kind) in _active:
        raise UserError("Этот ролик уже загружается.")
    profile = youtube.get_profile(channel_id)
    if not profile:
        raise UserError("Канал не найден. Подключите его в Настройках → YouTube.")
    privacy = privacy or profile["privacy"]
    channel = next((c for c in youtube.list_channels() if c["id"] == channel_id), {})
    _active.add((jid, kind))
    _set(jid, kind, status="queued", progress=0, error="", channel_id=channel_id,
         channel_title=channel.get("title", ""), privacy=privacy, publish_at=publish_at, note="")
    threading.Thread(target=_run, args=(jid, kind, channel_id, privacy, publish_at, profile), daemon=True).start()


def _run(jid, kind, channel_id, privacy, publish_at, profile):
    try:
        with _one_at_a_time:
            _set(jid, kind, status="uploading", progress=0)
            d = pipeline.job_dir(jid)
            extras = pipeline.read_json(d / "extras.json") or {}
            job = db.get(jid) or {"options": {}}
            lang = pipeline.job_language(job)
            tags = extras.get("tags", [])
            short_format = job["options"].get("format") == "short"
            if kind == "video":
                title, desc, file = extras.get("youtube_title", ""), extras.get("description", ""), d / "output" / "video.mp4"
                if short_format:
                    if "#shorts" not in title.lower():
                        title = f"{title} #shorts"
                    if "#shorts" not in desc.lower():
                        desc = f"{desc}\n\n#shorts"
            else:
                short = pipeline.read_json(d / "short_script.json") or {}
                title = f"{short.get('title', '')} #shorts"
                more = "Полная история на нашем канале." if lang == "ru" else "The full story is on our channel."
                desc = (f"{short.get('title', '')}\n{more}\n\n#shorts "
                        + " ".join("#" + "".join(t.split()) for t in tags[:3]))
                file = d / "output" / "short.mp4"
            _log(jid, f"YouTube: загружаю {'видео' if kind == 'video' else 'шортс'} на канал «{state(jid).get(kind, {}).get('channel_title', '')}»…")
            last = [0.0]

            def progress(p):
                if p - last[0] >= 0.02 or p >= 1:
                    last[0] = p
                    _set(jid, kind, progress=round(p * 100, 1))

            vid = youtube.upload(channel_id, file, title, desc, tags, privacy, publish_at,
                                 profile.get("category", "27"), profile.get("synthetic", True), progress, lang,
                                 bool(projects.get(job["options"].get("project_id", "")).get("made_for_kids")))
            note = ""
            thumb = d / "output" / "thumbnail.jpg"
            if kind == "video" and thumb.exists():
                try:
                    youtube.set_thumbnail(channel_id, vid, thumb)
                    _log(jid, "YouTube: превью поставлено")
                except UserError as e:
                    note = thumb_note(e)
                    _log(jid, f"YouTube: превью не принято — {e}")
            url = f"https://youtu.be/{vid}" if kind == "video" else f"https://youtube.com/shorts/{vid}"
            _set(jid, kind, status="done", progress=100, video_id=vid, url=url, note=note, uploaded_at=time.time())
            _log(jid, f"YouTube: готово, {url}")
    except UserError as e:
        _set(jid, kind, status="error", error=str(e))
        _log(jid, f"YouTube: ошибка — {e}")
    except Exception as e:
        _set(jid, kind, status="error", error=f"Непредвиденная ошибка: {e}")
        _log(jid, f"YouTube: непредвиденная ошибка — {e}")
    finally:
        _active.discard((jid, kind))


def _reel_file(jid: str):
    """The vertical video of a job: the Short itself, or the Short made next to a long video."""
    out = pipeline.job_dir(jid) / "output"
    job = db.get(jid) or {}
    if job.get("options", {}).get("format") == "short" and (out / "video.mp4").exists():
        return out / "video.mp4"
    return out / "short.mp4" if (out / "short.mp4").exists() else None


def start_instagram(jid: str):
    file = _reel_file(jid)
    if not file:
        raise UserError("Вертикального видео для Instagram у этого ролика нет.")
    if (jid, "instagram") in _active:
        raise UserError("Этот ролик уже выкладывается в Instagram.")
    instagram._auth()
    _active.add((jid, "instagram"))
    _set(jid, "instagram", status="queued", progress=0, error="", note="")
    threading.Thread(target=_run_instagram, args=(jid, file), daemon=True).start()


def _with_cover(jid, file):
    """The Reel with the program's cover as its first frame; the plain file if there is no cover."""
    d = pipeline.job_dir(jid)
    vertical = ((db.get(jid) or {}).get("options") or {}).get("format") == "short"
    cover = d / "output" / "thumbnail.jpg"
    if not vertical or file.name != "video.mp4":
        # A long video's thumbnail is wide: its Reel (the Short) gets a vertical copy of it.
        src, cover = d / "images" / "thumbnail_src.png", d / "output" / "ig_cover.jpg"
        if not src.exists():
            return file
        opts = pipeline.read_json(d / "thumb.json", {}) or {}
        extras = pipeline.read_json(d / "extras.json", {}) or {}
        thumb.render(src, cover, opts.get("text", extras.get("thumbnail_text", "")), opts.get("side", "left"), True)
    if not cover.exists():
        return file
    _set(jid, "instagram", step="ставлю обложку")
    out = d / "output" / "ig_upload.mp4"
    try:
        media.prepend_cover(file, cover, out)
    except UserError as e:
        _log(jid, f"Instagram: обложку поставить не вышло, выкладываю без неё — {e}")
        return file
    return out


def _run_instagram(jid, file):
    try:
        with _one_at_a_time:
            extras = pipeline.read_json(pipeline.job_dir(jid) / "extras.json", {}) or {}
            _set(jid, "instagram", status="uploading", step="готовлю")
            _log(jid, "Instagram: выкладываю рилс…")
            send = _with_cover(jid, file)
            try:
                r = instagram.publish(send, instagram.caption(extras), say=lambda m: _set(jid, "instagram", step=m),
                                      cover=send != file)
            finally:
                if send != file:
                    send.unlink(missing_ok=True)
            _set(jid, "instagram", status="done", step="", media_id=r["media_id"], url=r["url"], uploaded_at=time.time(),
                 username=instagram.load_settings().get("ig_username", ""))
            _log(jid, f"Instagram: готово, {r['url']}")
    except UserError as e:
        _set(jid, "instagram", status="error", error=str(e))
        _log(jid, f"Instagram: ошибка — {e}")
    except Exception as e:
        _set(jid, "instagram", status="error", error=f"Непредвиденная ошибка: {e}")
        _log(jid, f"Instagram: непредвиденная ошибка — {e}")
    finally:
        _active.discard((jid, "instagram"))


def auto(jid: str):
    """Called when a video is finished: upload it if its channel has auto-upload on."""
    job = db.get(jid)
    if not job:
        return
    s = instagram.load_settings()
    if s.get("ig_auto") and s.get("ig_token") and _reel_file(jid) and \
            (state(jid).get("instagram") or {}).get("status") != "done":
        try:
            start_instagram(jid)
        except UserError as e:
            _set(jid, "instagram", status="error", error=str(e))
    cid = job["options"].get("channel_id")
    if not cid:
        return
    profile = youtube.get_profile(cid)
    if not profile or not profile.get("auto_upload"):
        return
    s = state(jid)
    out = pipeline.job_dir(jid) / "output"
    for kind, name in (("video", "video.mp4"), ("short", "short.mp4")):
        if (out / name).exists() and (s.get(kind) or {}).get("status") != "done":
            try:
                start(jid, kind, cid, profile["privacy"], "")
            except UserError as e:
                _set(jid, kind, status="error", error=str(e))
