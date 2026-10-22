"""AI producer: takes a YouTube link (a video or a whole channel), an uploaded video or a pasted script, gets
the words that are spoken, and explains why the videos worked. The analyses can be saved into a niche's script
library, which the writer studies before writing new videos."""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx

from . import llm, media, net, remake
from .config import DATA_DIR, load_settings
from .net import UserError

DIR = DATA_DIR / "producer"
_threads = {}
_lock = threading.Lock()
YT_RE = re.compile(r"^https?://(www\.|m\.)?(youtube\.com|youtu\.be)/", re.I)


def _dir(pid: str) -> Path:
    if not re.fullmatch(r"[a-z0-9]{6,32}", pid or ""):
        raise UserError("Разбор не найден.")
    return DIR / pid


def _read(p: Path, default=None):
    try:
        return json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def _write(p: Path, data):
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(p)


def _log(d: Path, text: str):
    with open(d / "log.txt", "a", encoding="utf-8") as f:
        f.write(time.strftime("%H:%M:%S ") + text + "\n")


def get(aid: str) -> dict:
    d = _dir(aid)
    m = _read(d / "meta.json")
    if not m:
        raise UserError("Разбор не найден.")
    m["log"] = (d / "log.txt").read_text("utf-8")[-6000:] if (d / "log.txt").exists() else ""
    if m.get("status") == "running" and aid not in _threads:
        m.update(status="error", error="Работа прервалась (программа закрывалась). Запустите разбор ещё раз.")
    return m


def list_all() -> list:
    if not DIR.exists():
        return []
    out = []
    for d in DIR.iterdir():
        m = _read(d / "meta.json")
        if m:
            out.append({"id": m["id"], "created": m.get("created"), "status": m.get("status"), "source": m.get("source"),
                        "title": m.get("title") or m.get("source"), "count": len(m.get("items") or [])})
    return sorted(out, key=lambda x: x.get("created") or 0, reverse=True)


def delete(aid: str):
    if aid in _threads:
        raise UserError("Подождите, пока разбор закончится.")
    shutil.rmtree(_dir(aid), ignore_errors=True)


def start(source: str = "", count: int = 5, text: str = "", file_bytes: bytes = None, file_name: str = "") -> dict:
    """source: a YouTube link; text: a pasted script; file_bytes: an uploaded video."""
    source = (source or "").strip()
    if not (source or (text or "").strip() or file_bytes):
        raise UserError("Вставьте ссылку на ролик или канал YouTube, загрузите видео или вставьте текст сценария.")
    if source and not YT_RE.match(source):
        raise UserError("Это не похоже на ссылку YouTube. Пример: https://www.youtube.com/shorts/… или https://www.youtube.com/@канал")
    aid = uuid.uuid4().hex[:12]
    d = DIR / aid
    d.mkdir(parents=True, exist_ok=True)
    kind = "file" if file_bytes else "text" if not source else "link"
    if file_bytes:
        (d / "upload.mp4").write_bytes(file_bytes)
    meta = {"id": aid, "created": time.time(), "status": "running", "kind": kind,
            "source": source or file_name or "Текст сценария", "count": max(1, min(int(count or 5), 10)),
            "text": (text or "").strip()[:30000], "items": [], "channel": None}
    _write(d / "meta.json", meta)
    t = threading.Thread(target=_run, args=(aid,), daemon=True)
    with _lock:
        _threads[aid] = t
    t.start()
    return get(aid)


def _run(aid: str):
    d = _dir(aid)
    try:
        _work(d)
        m = _read(d / "meta.json")
        m["status"] = "done"
        _write(d / "meta.json", m)
        _log(d, "Разбор готов.")
    except Exception as e:  # noqa: BLE001 - every failure is shown to the owner
        m = _read(d / "meta.json") or {}
        msg = str(e) if isinstance(e, UserError) else f"Непредвиденная ошибка: {e}"
        m.update(status="error", error=msg)
        _write(d / "meta.json", m)
        _log(d, "Ошибка: " + msg)
    finally:
        with _lock:
            _threads.pop(aid, None)


# ---------------------------------------------------------------- YouTube through yt-dlp

def _uv() -> str:
    return shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")


def update_downloader() -> str:
    """YouTube changes often; a fresh yt-dlp (and the deno engine it uses for YouTube) fixes most failures."""
    cmd = [_uv(), "pip", "install", "--python", sys.executable, "-q", "-U", "yt-dlp[default]"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp[default]"],
                           capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            raise UserError(f"Не удалось обновить загрузчик YouTube: {(r.stderr or r.stdout)[-300:]}")
    # deno helps yt-dlp read YouTube; not fatal when it cannot be installed
    subprocess.run([_uv(), "pip", "install", "--python", sys.executable, "-q", "-U", "deno"],
                   capture_output=True, text=True, timeout=900)
    return _ytdlp_version()


def _ytdlp_version() -> str:
    r = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"], capture_output=True, text=True, timeout=60)
    return r.stdout.strip() if r.returncode == 0 else ""


BROWSERS = {"chrome": "Google Chrome", "firefox": "Firefox", "safari": "Safari", "edge": "Microsoft Edge",
            "brave": "Brave Browser", "opera": "Opera", "vivaldi": "Vivaldi", "chromium": "Chromium"}
BOT_MARK = "Sign in to confirm"
_auto_failed = [0.0]  # when trying every browser last failed: not again for 10 minutes


def _installed_browsers() -> list:
    """Browsers to borrow the YouTube sign-in from; Firefox first (no password prompt), Safari last (needs Full
    Disk Access)."""
    order = ["firefox", "chrome", "edge", "brave", "opera", "vivaldi", "chromium", "safari"]
    if sys.platform != "darwin":
        return [b for b in order if b != "safari"]
    apps = [Path("/Applications"), Path.home() / "Applications"]
    return [b for b in order if b == "safari" or any((a / f"{BROWSERS[b]}.app").exists() for a in apps)]


def _ytdlp_once(args: list, timeout: int, browser: str = "") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")  # deno from the venv
    env["PATH"] += os.pathsep + str(Path(media.ffmpeg()).parent)
    extra = ["--cookies-from-browser", browser] if browser else []
    try:
        return subprocess.run([sys.executable, "-m", "yt_dlp", "--no-warnings", "--ffmpeg-location", media.ffmpeg(),
                               *extra, *args], capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise UserError("YouTube слишком долго отвечает. Попробуйте ещё раз.")


def _cookie_error(err: str) -> bool:
    e = err.lower()
    return BOT_MARK.lower() not in e and ("cookie" in e or "keyring" in e or "decrypt" in e or "operation not permitted" in e)


def _ytdlp(args: list, timeout: int = 300) -> str:
    """Runs yt-dlp. When YouTube takes the program for a robot, it borrows the owner's YouTube sign-in from his
    browser (the way his own browser watches YouTube) and remembers which browser worked."""
    browser = load_settings().get("yt_cookies_browser", "") or ""
    r = _ytdlp_once(args, timeout, browser if browser not in ("", "none") else "")
    err = (r.stderr or r.stdout).strip() if r.returncode else ""
    if r.returncode and browser and browser != "none" and _cookie_error(err):
        name = BROWSERS.get(browser, browser)
        hint = ("Safari: Системные настройки → Конфиденциальность и безопасность → Полный доступ к диску → включите "
                "«Авто ютуб» (или Terminal/Python). Либо выберите другой браузер." if browser == "safari" else
                f"Откройте {name} и проверьте, что вы вошли в YouTube. Если Mac спросит пароль для «{name} Safe Storage», "
                "нажмите «Разрешать всегда».")
        raise UserError(f"Не удалось взять вход в YouTube из браузера {name}. {hint} Подробности: {err[-200:]}")
    if r.returncode and BOT_MARK in err and not browser and time.time() - _auto_failed[0] > 600:
        _auto_failed[0] = time.time()
        for b in _installed_browsers():
            r2 = _ytdlp_once(args, timeout, b)
            if r2.returncode == 0:
                from .config import save_settings
                save_settings({"yt_cookies_browser": b})
                _auto_failed[0] = 0.0
                return r2.stdout
        err += "\n(вход из браузеров не помог)"
    if r.returncode:
        if "No module named yt_dlp" in err:
            raise UserError("Загрузчик YouTube не установлен. Нажмите «Обновить загрузчик YouTube» на этой странице.")
        if "Private video" in err or "members-only" in err.lower():
            raise UserError("Это видео закрыто или только для спонсоров: разобрать его нельзя.")
        if BOT_MARK in err:
            raise UserError("YouTube принял программу за робота. Откройте YouTube в Chrome, Firefox или Safari и войдите "
                            "в свой аккаунт, затем внизу страницы «ИИ продюсер» выберите этот браузер в поле «Вход в "
                            "YouTube» и запустите разбор ещё раз. Если вы с VPN, попробуйте другую страну или без VPN.")
        raise UserError("YouTube не отдал ролик. Нажмите «Обновить загрузчик YouTube» и попробуйте ещё раз. "
                        f"Подробности: {err[-300:]}")
    return r.stdout


def video_info(url: str) -> dict:
    return json.loads(_ytdlp(["-J", "--skip-download", "--no-playlist", url]))


def channel_videos(url: str, count: int) -> tuple:
    """(channel title, followers, [entries]) with the most viewed videos of the channel's Shorts and videos."""
    base = re.sub(r"/(shorts|videos|streams|featured|about)/?$", "", url.split("?")[0].rstrip("/"))
    entries, title, subs = [], "", None
    for tab in ("shorts", "videos"):
        try:
            data = json.loads(_ytdlp(["-J", "--flat-playlist", "--playlist-end", "60", f"{base}/{tab}"], 300))
        except (UserError, ValueError):
            continue
        title = title or data.get("channel") or data.get("uploader") or data.get("title") or ""
        subs = subs or data.get("channel_follower_count")
        for e in data.get("entries") or []:
            if e.get("id"):
                entries.append({"id": e["id"], "title": e.get("title", ""), "views": e.get("view_count") or 0,
                                "url": e.get("url") if str(e.get("url", "")).startswith("http")
                                else f"https://www.youtube.com/{'shorts/' if tab == 'shorts' else 'watch?v='}{e['id']}",
                                "tab": tab})
    if not entries:
        raise UserError("Не нашёл роликов на этом канале. Проверьте ссылку: нужна ссылка вида https://www.youtube.com/@канал")
    entries.sort(key=lambda e: e["views"] or 0, reverse=True)
    return title, subs, entries[:count]


def _is_channel(url: str) -> bool:
    return bool(re.search(r"youtube\.com/(@|channel/|c/|user/)", url, re.I)) and "/watch" not in url and "/shorts/" not in url


def _captions(info: dict) -> list:
    """[(start seconds, text)] from YouTube subtitles: the author's own first, then automatic ones."""
    lang = (info.get("language") or "").split("-")[0]
    for group in ("subtitles", "automatic_captions"):
        tracks = info.get(group) or {}
        keys = list(tracks)
        order = [k for k in keys if k.endswith("-orig")] + [k for k in keys if lang and k.split("-")[0] == lang] \
            + [k for k in keys if k.split("-")[0] in ("ru", "en")] + (keys if group == "subtitles" else [])
        for k in order:
            fmt = next((f for f in tracks.get(k) or [] if f.get("ext") == "json3"), None)
            if not fmt:
                continue
            try:
                data = httpx.get(fmt["url"], timeout=60, follow_redirects=True).json()
            except (httpx.HTTPError, ValueError):
                continue
            out = []
            for ev in data.get("events") or []:
                t = "".join(s.get("utf8", "") for s in ev.get("segs") or []).replace("\n", " ").strip()
                if t:
                    out.append((ev.get("tStartMs", 0) / 1000, t))
            if out:
                return out
    return []


def _download(url: str, d: Path, short: bool) -> Path:
    """A small copy of the video (for frames and, when there are no subtitles, for the speech)."""
    fmt = "worst[ext=mp4][height>=240]/worst[ext=mp4]/worst" if short else "worstaudio/worst"
    _ytdlp(["-f", fmt, "--no-playlist", "-o", str(d / "src.%(ext)s"), url], 900)
    files = sorted(d.glob("src.*"))
    if not files:
        raise UserError("Не удалось скачать ролик.")
    return files[0]


# ---------------------------------------------------------------- analysis

def _frames(video: Path, duration: float, d: Path, n: int = 6) -> list:
    out = []
    for i in range(n):
        t = duration * (i + 0.5) / n if duration else i
        f = d / f"frame_{i}.jpg"
        try:
            media.run(["-ss", f"{t:.2f}", "-i", video, "-frames:v", 1, "-vf", "scale=360:-2", "-q:v", 5, f], "Кадр")
            out.append(f.read_bytes())
        except UserError:
            pass
        f.unlink(missing_ok=True)
    return out


def _thumb(url: str, d: Path) -> list:
    if not url:
        return []
    try:
        raw = httpx.get(url, timeout=30, follow_redirects=True).content
        src, jpg = d / "thumb.src", d / "thumb.jpg"
        src.write_bytes(raw)
        media.run(["-i", src, "-vf", "scale=640:-2", jpg], "Обложка")
        data = jpg.read_bytes()
        src.unlink(missing_ok=True)
        return [data]
    except (httpx.HTTPError, UserError, OSError):
        return []


def _timed(lines: list) -> str:
    return "\n".join(f"[{int(t // 60)}:{int(t % 60):02d}] {x}" for t, x in lines)


def _plain(lines: list) -> str:
    return re.sub(r"\s+", " ", " ".join(x for _, x in lines)).strip()


SCHEMA_ANALYSIS = {"type": "object", "properties": {
    "summary_ru": {"type": "string"}, "hook_ru": {"type": "string"}, "structure_ru": {"type": "array", "items": {"type": "string"}},
    "why_ru": {"type": "string"}, "strengths_ru": {"type": "array", "items": {"type": "string"}},
    "weaknesses_ru": {"type": "array", "items": {"type": "string"}}, "packaging_ru": {"type": "string"},
    "techniques": {"type": "array", "items": {"type": "string"}}, "topics_ru": {"type": "array", "items": {"type": "string"}},
    "script_ru": {"type": "string"}},
    "required": ["summary_ru", "hook_ru", "structure_ru", "why_ru", "strengths_ru", "techniques", "topics_ru"]}

ANALYSIS_PROMPT = """You are a top YouTube producer who has grown many channels. Break down the video below and explain, concretely and honestly, why it works (or where it is weak). The owner will use your analysis to make his own videos in this niche, and a scriptwriter will study it.

{meta}

{frames_note}

WHAT IS SAID IN THE VIDEO (with times):
<<<
{transcript}
>>>

Return:
- summary_ru: what the video is about and how it goes, 2-4 sentences in Russian.
- hook_ru: the first line or first seconds word for word, then why exactly it stops the scroll (Russian).
- structure_ru: the beats of the video in order, each as "0:05 — what happens and its job for the viewer" (Russian).
- why_ru: the main reasons it got its views: emotion, curiosity, identity, conflict, novelty, the format, the character, the timing (Russian, 3-6 sentences, with numbers from the stats when they help: views per subscriber, likes per view).
- strengths_ru: 4-8 concrete strengths a creator can copy (Russian).
- weaknesses_ru: 1-4 things that could be better (Russian).
- packaging_ru: what the title and thumbnail do and why they make people click (Russian).
- techniques: 5-10 short rules in English for a scriptwriter, each a reusable technique, not a retelling of this plot (for example "Open with the punchline image, explain it only after 3 seconds").
- topics_ru: 5-8 new topic ideas in the same spirit for a channel in this niche (Russian), never copies of this plot.
- script_ru: if the speech is not Russian, its Russian translation; otherwise an empty string."""


def analyse(settings: dict, usage: dict, meta_text: str, transcript: str, images: list) -> dict:
    note = (f"You also see {len(images)} pictures: the first is the thumbnail when there is one, the rest are frames from "
            "the video in time order. Use them to judge the visuals, the framing, the on-screen text and the pace."
            if images else "No pictures of the video are available: judge by the words and the stats.")
    user = ANALYSIS_PROMPT.format(meta=meta_text, frames_note=note, transcript=transcript[:20000] or "(no speech found)")
    return llm.call_json(settings, "You are a YouTube producer and script analyst. Answer only with JSON.", user,
                         SCHEMA_ANALYSIS, usage, 8000, images=images or None)


SCHEMA_CHANNEL = {"type": "object", "properties": {
    "summary_ru": {"type": "string"}, "formula_ru": {"type": "string"}, "strengths_ru": {"type": "array", "items": {"type": "string"}},
    "techniques": {"type": "array", "items": {"type": "string"}}, "topics_ru": {"type": "array", "items": {"type": "string"}},
    "advice_ru": {"type": "string"}}, "required": ["summary_ru", "formula_ru", "strengths_ru", "techniques", "topics_ru"]}


def _meta_text(info: dict) -> str:
    v, subs = info.get("view_count"), info.get("channel_follower_count")
    rows = [f"Title: {info.get('title', '')}", f"Channel: {info.get('channel') or info.get('uploader') or ''}"
            + (f" ({subs} subscribers)" if subs else ""),
            f"Duration: {int(info.get('duration') or 0)} s", f"Published: {info.get('upload_date', '')}",
            f"Views: {v}" if v is not None else "", f"Likes: {info.get('like_count')}" if info.get("like_count") else "",
            f"Comments: {info.get('comment_count')}" if info.get("comment_count") else "",
            f"Tags: {', '.join((info.get('tags') or [])[:20])}" if info.get("tags") else "",
            f"Description: {(info.get('description') or '')[:800]}" if info.get("description") else ""]
    return "VIDEO STATS:\n" + "\n".join(r for r in rows if r)


def _item(info: dict, analysis: dict, transcript: str, url: str) -> dict:
    dur = int(info.get("duration") or 0)
    return {"source": url, "title": info.get("title", ""), "channel": info.get("channel") or info.get("uploader") or "",
            "views": info.get("view_count"), "likes": info.get("like_count"), "duration": dur,
            "kind": "short" if (dur and dur <= 180) or "/shorts/" in (url or "") else "video",
            "thumbnail": info.get("thumbnail", ""), "script": transcript, **analysis}


def _one_video(d: Path, settings: dict, usage: dict, url: str, n: int) -> dict:
    sub = d / f"v{n}"
    sub.mkdir(exist_ok=True)
    _log(d, f"Получаю данные ролика {url}…")
    info = video_info(url)
    dur = float(info.get("duration") or 0)
    short = dur <= 180 or "/shorts/" in url
    lines = _captions(info)
    images = _thumb(info.get("thumbnail", ""), sub)
    video = None
    if short or not lines:
        try:
            video = _download(url, sub, short)
        except UserError as e:
            _log(d, f"Видео скачать не получилось ({e}). Разбираю по тексту и обложке.")
    if video and short:
        images += _frames(video, dur, sub)
    if not lines and video:
        _log(d, "Субтитров у ролика нет, распознаю речь (ElevenLabs)…")
        mp3 = sub / "speech.mp3"
        media.run(["-i", video, "-vn", "-ac", 1, "-ar", 22050, "-b:a", "64k", mp3], "Звук")
        tr = remake.transcribe(settings, mp3)
        segs = remake.segments(tr.get("words") or [])
        lines = [(s["start"], s["text"]) for s in segs]
    shutil.rmtree(sub, ignore_errors=True)
    _log(d, f"Разбираю «{info.get('title', '')}»…")
    a = analyse(settings, usage, _meta_text(info), _timed(lines), images)
    return _item(info, a, _plain(lines), info.get("webpage_url") or url)


def _work(d: Path):
    m = _read(d / "meta.json")
    settings = load_settings()
    usage = {}
    items = []
    if m["kind"] == "text":
        _log(d, "Разбираю вставленный текст…")
        a = analyse(settings, usage, "VIDEO STATS: unknown (the owner pasted the script).", m["text"], [])
        items.append({"source": "", "title": a.get("summary_ru", "")[:80], "kind": "short" if len(m["text"].split()) < 250 else "video",
                      "script": m["text"], **a})
    elif m["kind"] == "file":
        src = d / "upload.mp4"
        info = remake._probe(src)
        _log(d, "Распознаю речь (ElevenLabs)…")
        mp3 = d / "speech.mp3"
        media.run(["-i", src, "-vn", "-ac", 1, "-ar", 22050, "-b:a", "64k", mp3], "Звук")
        tr = remake.transcribe(settings, mp3)
        lines = [(s["start"], s["text"]) for s in remake.segments(tr.get("words") or [])]
        images = _frames(src, info["duration"], d, 8)
        _log(d, "Разбираю ролик…")
        meta = {"title": m["source"], "duration": info["duration"]}
        a = analyse(settings, usage, _meta_text(meta), _timed(lines), images)
        items.append(_item(meta, a, _plain(lines), ""))
        for f in (src, mp3):
            f.unlink(missing_ok=True)
    elif _is_channel(m["source"]):
        _log(d, "Ищу самые просматриваемые ролики канала…")
        title, subs, entries = channel_videos(m["source"], m["count"])
        m.update(title=f"Канал {title}" if title else m["source"], subscribers=subs)
        _write(d / "meta.json", m)
        _log(d, f"Беру {len(entries)} роликов с наибольшими просмотрами.")
        for i, e in enumerate(entries):
            try:
                items.append(_one_video(d, settings, usage, e["url"], i))
            except UserError as err:
                _log(d, f"Ролик «{e['title']}» пропущен: {err}")
            m["items"] = items
            _write(d / "meta.json", m)
        if not items:
            raise UserError("Ни один ролик канала разобрать не удалось.")
        _log(d, "Ищу общую формулу канала…")
        brief = "\n\n".join(f"### {x['title']} ({x.get('views')} views)\nWhy: {x.get('why_ru', '')}\nHook: {x.get('hook_ru', '')}\n"
                            f"Techniques: {'; '.join(x.get('techniques') or [])}" for x in items)
        m["channel"] = llm.call_json(settings, "You are a YouTube producer. Answer only with JSON.",
                                     f"Channel: {title} ({subs} subscribers). Analyses of its most viewed videos:\n\n{brief}\n\n"
                                     "Return: summary_ru (what the channel does, for whom, Russian); formula_ru (the repeatable "
                                     "formula of its hits: topic choice, hook, structure, character, packaging; Russian, 4-8 "
                                     "sentences); strengths_ru (Russian list); techniques (English rules for a scriptwriter); "
                                     "topics_ru (8-12 new topic ideas in the same spirit, Russian); advice_ru (how to beat this "
                                     "channel with our own videos, Russian, 3-5 sentences).", SCHEMA_CHANNEL, usage, 6000)
    else:
        items.append(_one_video(d, settings, usage, m["source"], 0))
        m["title"] = items[0]["title"]
    m = {**_read(d / "meta.json"), "items": items, "channel": m.get("channel"), "title": m.get("title") or items[0].get("title"),
         "usage": usage}
    _write(d / "meta.json", m)
    _log(d, f"Потрачено на разбор: ~${usage.get('llm_cost', 0):.2f}")


def library_entries(aid: str, indexes: list = None, with_channel: bool = True) -> list:
    """Entries for the niche library from a finished analysis."""
    m = get(aid)
    items = m.get("items") or []
    pick = [items[i] for i in (indexes if indexes is not None else range(len(items))) if 0 <= i < len(items)]
    out = [{k: x.get(k) for k in ("source", "title", "channel", "views", "likes", "duration", "kind", "thumbnail", "script",
                                    "summary_ru", "hook_ru", "structure_ru", "why_ru", "strengths_ru", "weaknesses_ru",
                                    "packaging_ru", "techniques", "topics_ru", "script_ru")} | {"type": "video"} for x in pick]
    ch = m.get("channel")
    if with_channel and ch:
        out.append({"type": "channel", "source": m.get("source"), "title": m.get("title", ""),
                    "summary_ru": ch.get("summary_ru", ""), "why_ru": ch.get("formula_ru", ""),
                    "strengths_ru": ch.get("strengths_ru") or [], "techniques": ch.get("techniques") or [],
                    "topics_ru": ch.get("topics_ru") or [], "advice_ru": ch.get("advice_ru", ""), "kind": "channel"})
    return out


SCHEMA_PLAYBOOK = {"type": "object", "properties": {"playbook_ru": {"type": "string"}, "playbook": {"type": "string"}},
                   "required": ["playbook_ru", "playbook"]}


def build_playbook(settings: dict, niche: str, items: list) -> dict:
    """The rules of this niche distilled from every saved analysis."""
    if not items:
        raise UserError("В хранилище этой ниши пока нет разборов.")
    brief = "\n\n".join(f"### {x.get('title', '')} ({x.get('views') or '?'} views, {x.get('kind', '')})\n"
                        f"Why it worked: {x.get('why_ru', '')}\nHook: {x.get('hook_ru', '')}\n"
                        f"Techniques: {'; '.join(x.get('techniques') or [])}" for x in items[-40:])
    user = (f"Channel niche (the owner's description, may be in Russian):\n{niche[:3000]}\n\nAnalyses of videos that "
            f"worked in this niche:\n\n{brief}\n\nDistil the playbook of this niche: what topics win, how hooks are "
            "built, the structure and rhythm, the tone and the character, packaging (titles, thumbnails), and the "
            "mistakes to avoid. playbook_ru: for the owner, in Russian, 10-20 short numbered rules. playbook: the same "
            "rules in English for the scriptwriter, imperative and concrete.")
    return llm.call_json(settings, "You are a YouTube producer. Answer only with JSON.", user, SCHEMA_PLAYBOOK, {}, 6000)
