"""Remake of someone else's finished video: the Russian speech is recognised, translated, voiced anew, the old
burned-in subtitles are hidden or erased, and new subtitles, title, description and tags are made."""
import json
import re
import shutil
import subprocess
import threading
import time
import uuid
import wave
from pathlib import Path

from . import captions, llm, media, music as musiclib, net, thumb, tts
from .config import DATA_DIR, load_settings
from .net import UserError

DIR = DATA_DIR / "remakes"
_threads = {}
_lock = threading.Lock()

SUBS_MODES = {"blur": "Закрыть размытой полосой (бесплатно)", "ai": "Стереть нейросетью (Replicate, платно)",
              "none": "В ролике нет субтитров"}
LANGS = {"en": "English"}
STT_MODELS = ("scribe_v2", "scribe_v1")
REPLICATE_MODEL = "hjunior29/video-text-remover"


def _dir(rid: str) -> Path:
    if not re.fullmatch(r"[a-z0-9]{6,32}", rid or ""):
        raise UserError("Переделка не найдена.")
    return DIR / rid


def _read(p: Path, default=None):
    try:
        return json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def _write(p: Path, data):
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(p)


def get(rid: str) -> dict:
    d = _dir(rid)
    meta = _read(d / "meta.json")
    if not meta:
        raise UserError("Переделка не найдена.")
    meta["log"] = (d / "log.txt").read_text("utf-8")[-8000:] if (d / "log.txt").exists() else ""
    meta["files"] = {n: (d / "output" / n).exists() for n in ("video.mp4", "thumbnail.jpg", "youtube.txt", "subtitles.srt")}
    tr, lines = _read(d / "transcript.json") or {}, _read(d / "lines.json") or []
    segs = segments(tr.get("words") or []) if lines else []
    meta["preview_lines"] = [{"start": s["start"], "end": s["end"], "text": t} for s, t in zip(segs, lines)] \
        if len(segs) == len(lines) else []
    if meta.get("status") == "running" and rid not in _threads:
        meta["status"] = "error"  # the program was closed while it worked
        meta["error"] = "Работа прервалась (программа закрывалась). Нажмите «Запустить» ещё раз."
    return meta


def list_all() -> list:
    if not DIR.exists():
        return []
    out = []
    for d in DIR.iterdir():
        m = _read(d / "meta.json")
        if m:
            out.append({k: m.get(k) for k in ("id", "name", "created", "status", "duration", "title")})
    return sorted(out, key=lambda m: m.get("created") or 0, reverse=True)


def _probe(path: Path) -> dict:
    p = subprocess.run([media.ffmpeg(), "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", p.stderr)
    if not m:
        raise UserError("Это не похоже на видео. Загрузите файл mp4.")
    rot = re.search(r"rotat\w*\s*:\s*-?90|rotation of -?90", p.stderr)
    w, h = int(m.group(1)), int(m.group(2))
    if rot:
        w, h = h, w
    return {"width": w, "height": h, "duration": media.video_duration(path), "has_audio": "Audio:" in p.stderr}


def create(name: str, data: bytes) -> dict:
    rid = uuid.uuid4().hex[:12]
    d = DIR / rid
    (d / "output").mkdir(parents=True, exist_ok=True)
    src = d / "source.mp4"
    src.write_bytes(data)
    try:
        info = _probe(src)
    except UserError:
        shutil.rmtree(d, ignore_errors=True)
        raise
    if not info["has_audio"]:
        shutil.rmtree(d, ignore_errors=True)
        raise UserError("В видео нет звука: переводить нечего.")
    media.run(["-ss", min(1.0, info["duration"] / 3), "-i", src, "-frames:v", 1, "-q:v", 3, d / "preview.jpg"], "Кадр")
    meta = {"id": rid, "name": name or "video.mp4", "created": time.time(), "status": "new", **info,
            "box": {"y": 70, "h": 12} if info["height"] > info["width"] else {"y": 80, "h": 14}}
    _write(d / "meta.json", meta)
    return get(rid)


def delete(rid: str):
    if rid in _threads:
        raise UserError("Подождите, пока переделка закончится.")
    shutil.rmtree(_dir(rid), ignore_errors=True)


def start(rid: str, opts: dict):
    d = _dir(rid)
    meta = _read(d / "meta.json")
    if not meta:
        raise UserError("Переделка не найдена.")
    with _lock:
        if rid in _threads:
            raise UserError("Переделка уже идёт.")
        if not (opts.get("voice_id") or "").strip():
            raise UserError("Выберите голос.")
        mode = opts.get("subs_mode") if opts.get("subs_mode") in SUBS_MODES else "blur"
        if mode == "ai" and not load_settings().get("replicate_key"):
            raise UserError("Для стирания субтитров нейросетью нужен ключ Replicate в Настройках. "
                            "Или выберите «Закрыть размытой полосой».")
        if (meta.get("opts") or {}).get("voice_id") != opts["voice_id"].strip():
            shutil.rmtree(d / "audio", ignore_errors=True)  # another voice: the phrases are voiced anew
        box = opts.get("box") or meta.get("box") or {}
        meta.update(status="running", error="", started=time.time(), opts={
            "voice_id": opts["voice_id"].strip(), "voice_name": (opts.get("voice_name") or "").strip(),
            "lang": "en", "subs_mode": mode, "restyle": bool(opts.get("restyle", True)),
            "music": "off" if opts.get("music") == "off" else "library", "new_subs": bool(opts.get("new_subs", True)),
            "subs_scale": _num(opts.get("subs_scale"), 1.0, 0.4, 2.5),
            "subs_pos": None if opts.get("subs_pos") in (None, "") else _num(opts.get("subs_pos"), 80, 5, 98),
        }, box={"y": max(0.0, min(float(box.get("y", 75)), 95.0)), "h": max(3.0, min(float(box.get("h", 12)), 60.0))})
        _write(d / "meta.json", meta)
        (d / "log.txt").write_text("", "utf-8")
        t = threading.Thread(target=_run, args=(rid,), daemon=True)
        _threads[rid] = t
    t.start()


def _log(d: Path, text: str):
    with open(d / "log.txt", "a", encoding="utf-8") as f:
        f.write(time.strftime("%H:%M:%S ") + text + "\n")


def _run(rid: str):
    d = _dir(rid)
    try:
        _work(d)
        meta = _read(d / "meta.json")
        meta.update(status="done", finished=time.time())
        _write(d / "meta.json", meta)
        _log(d, "Готово.")
    except Exception as e:  # noqa: BLE001 - every failure is shown to the owner
        meta = _read(d / "meta.json") or {}
        msg = str(e) if isinstance(e, UserError) else f"Непредвиденная ошибка: {e}"
        meta.update(status="error", error=msg)
        _write(d / "meta.json", meta)
        _log(d, "Ошибка: " + msg)
    finally:
        with _lock:
            _threads.pop(rid, None)


# ---------------------------------------------------------------- steps

def transcribe(settings: dict, audio: Path) -> dict:
    """ElevenLabs speech-to-text with word timings."""
    last = None
    for model in STT_MODELS:
        try:
            with open(audio, "rb") as f:
                r = net.request("ElevenLabs", "POST", "https://api.elevenlabs.io/v1/speech-to-text",
                                headers={"xi-api-key": tts._key(settings)}, timeout=600, tries=3,
                                data={"model_id": model, "timestamps_granularity": "word", "tag_audio_events": "false"},
                                files={"file": (audio.name, f, "audio/mpeg")})
            return r.json()
        except UserError as e:
            if "ключ" in str(e) or "денег" in str(e) or "лимит" in str(e) or "кредит" in str(e):
                raise
            last = e
    raise last or UserError("ElevenLabs не распознал речь.")


def segments(words: list) -> list:
    """Phrases of the original speech: [{"text", "start", "end"}]. A phrase ends at a sentence end or a pause."""
    out, cur = [], []
    ws = [w for w in words if w.get("type", "word") == "word" and str(w.get("text", "")).strip()]
    for i, w in enumerate(ws):
        cur.append(w)
        nxt = ws[i + 1] if i + 1 < len(ws) else None
        end = re.search(r"[.!?…]\W*$", w["text"]) is not None
        pause = nxt is not None and float(nxt["start"]) - float(w["end"]) > 0.7
        if nxt is None or pause or (end and len(cur) >= 3) or len(cur) >= 25:
            out.append({"text": " ".join(x["text"].strip() for x in cur), "start": float(cur[0]["start"]),
                        "end": float(cur[-1]["end"])})
            cur = []
    return out


SCHEMA_LINES = {"type": "object", "properties": {"lines": {"type": "array", "items": {"type": "string"}}},
                "required": ["lines"]}
SCHEMA_META = {"type": "object", "properties": {
    "title": {"type": "string"}, "description": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}},
    "thumbnail_text": {"type": "string"}, "music_style_ru": {"type": "string"}, "title_ru": {"type": "string"}},
    "required": ["title", "description", "tags", "thumbnail_text"]}


def translate(settings: dict, segs: list, lang: str, usage: dict) -> list:
    target = LANGS.get(lang, "English")
    system = (f"You are a top YouTube localizer. You adapt Russian videos for a native {target}-speaking audience so "
              "they sound as if written in that language from the start: natural spoken phrasing, idioms, jokes and "
              "slang of the same strength, numbers in words. Answer only with JSON.")
    out = []
    for start in range(0, len(segs), 60):
        part = segs[start:start + 60]
        listing = "\n".join(f"{i}. ({s['end'] - s['start']:.1f} s) {s['text']}" for i, s in enumerate(part))
        user = (f"Translate the speech of this video into {target}, one line per numbered phrase: return exactly "
                f"{len(part)} lines in the same order, never merge or split them. Each phrase shows how many seconds "
                "it lasts in the video: the translation must be spoken in about that time (about 2.5 words per "
                "second), so shorten freely where needed while keeping the meaning, the joke and the punch.\n\n"
                + listing)
        for _ in range(2):
            r = llm.call_json(settings, system, user, SCHEMA_LINES, usage, 16000)
            got = [str(x).strip() for x in r.get("lines") or []]
            if len(got) == len(part):
                break
        else:
            raise UserError(f"Перевод вернул {len(got)} фраз вместо {len(part)}. Запустите ещё раз.")
        out += got
    return out


def metadata(settings: dict, text: str, lang: str, vertical: bool, usage: dict) -> dict:
    target = LANGS.get(lang, "English")
    user = (f"The {target} narration of a YouTube {'Short' if vertical else 'video'}:\n<<<\n{text}\n>>>\n\n"
            "Write the materials so that YouTube search and recommendations find it. First think of the phrases real "
            "people type into YouTube about this topic: the main phrase, synonyms, questions, related topics and "
            "broad mass phrases with a huge audience. Only phrases that truly fit.\n"
            f"- title: max {60 if vertical else 70} characters, the main search phrase in the first half, the rest "
            "curiosity-driven and honest, never reveals the twist;\n"
            "- description: 900-1500 characters of natural text (never a bare keyword list): the first sentence holds "
            "the main phrase and a hook, then 2-3 short paragraphs weaving in the search phrases, a short subscribe "
            "line, and on the last line 8-12 hashtags, the 3 most important first"
            + (", including #shorts" if vertical else "") + ";\n"
            "- tags: 25-35 tags, most important first: exact search phrases, broad mass tags, related topics; no '#';\n"
            "- thumbnail_text: 1-4 provocative words that make people click without revealing the answer;\n"
            "- title_ru: the title translated into Russian for the owner; music_style_ru: 2-5 Russian words for a "
            f"fitting background music style.\nEverything except title_ru and music_style_ru in {target}.")
    return llm.call_json(settings, "You are a YouTube SEO expert. Answer only with JSON.", user, SCHEMA_META, usage, 8000)


def _atempo(src: Path, dst: Path, factor: float):
    media.run(["-i", src, "-filter:a", f"atempo={factor:.3f}", "-ar", 44100, "-ac", 1, "-c:a", "pcm_s16le", dst],
              "Ускорение фразы")


def _silence(out: Path, seconds: float):
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\0\0" * int(max(seconds, 0.01) * 44100))


def build_voice(d: Path, settings: dict, segs: list, lines: list, lang: str, video_len: float, usage: dict) -> tuple:
    """Voices every phrase and places it where the original phrase began. A phrase that does not fit before the
    next one is spoken up to 1.3 times faster, then the rest moves later. Returns (scenes, durations, total)."""
    audio = d / "audio"
    audio.mkdir(exist_ok=True)
    scenes, parts = [{"text": ""}], []
    _silence(audio / "seg_000.wav", segs[0]["start"] if segs else 0.1)
    t = segs[0]["start"] if segs else 0.0
    placed = []
    for i, (s, text) in enumerate(zip(segs, lines), 1):
        mp3, wav = audio / f"seg_{i:03d}.mp3", audio / f"seg_{i:03d}.wav"
        if not wav.exists():
            chars = tts.synthesize(settings, text, mp3, "", "", lang)
            usage["tts_chars"] = usage.get("tts_chars", 0) + chars
            media.to_wav(mp3, wav)
            nxt = segs[i]["start"] if i < len(segs) else max(video_len, s["end"])
            slot = max(nxt - max(s["start"], t), 0.3)
            dur = media.wav_duration(wav)
            if dur > slot * 1.02:
                factor = min(1.3, dur / slot)
                fast = wav.with_suffix(".fast.wav")
                _atempo(wav, fast, factor)
                fast.replace(wav)
                al = mp3.with_suffix(".json")
                if al.exists():  # word timings follow the faster speech, so subtitles stay in sync
                    a = json.loads(al.read_text("utf-8"))
                    for k in ("character_start_times_seconds", "character_end_times_seconds"):
                        a[k] = [x / factor for x in a.get(k, [])]
                    al.write_text(json.dumps(a), "utf-8")
        start = max(s["start"], t)
        dur = media.wav_duration(wav)
        placed.append((wav, start, dur))
        t = start + dur
        scenes.append({"text": text})
    # Gaps after each phrase up to the next one's start.
    parts.append((audio / "seg_000.wav", 0.0))
    for k, (wav, start, dur) in enumerate(placed):
        nxt = placed[k + 1][1] if k + 1 < len(placed) else start + dur + 0.3
        parts.append((wav, max(nxt - start - dur, 0.0)))
    durations = media.concat_wavs(parts, d / "narration.wav")  # subtitles read the word timings next to each wav
    return scenes, durations, sum(durations)


def _replicate_erase(d: Path, settings: dict, src: Path) -> Path:
    """Erases burned-in subtitles with a Replicate model. The file goes to the owner's own Replicate storage."""
    key = settings.get("replicate_key")
    headers = {"Authorization": f"Bearer {key}"}
    with open(src, "rb") as f:
        up = net.request("Replicate", "POST", "https://api.replicate.com/v1/files", headers=headers, timeout=600,
                         tries=3, files={"content": ("video.mp4", f, "video/mp4")}).json()
    url = (up.get("urls") or {}).get("get")
    if not url:
        raise UserError("Replicate не принял видео.")
    model = net.request("Replicate", "GET", f"https://api.replicate.com/v1/models/{REPLICATE_MODEL}",
                        headers=headers, timeout=60).json()
    version = (model.get("latest_version") or {}).get("id")
    if not version:
        raise UserError("Модель стирания субтитров на Replicate недоступна. Выберите «Закрыть размытой полосой».")
    data = net.request("Replicate", "POST", "https://api.replicate.com/v1/predictions", headers=headers, timeout=120,
                       json={"version": version, "input": {"video": url, "method": "hybrid"}}).json()
    deadline = time.time() + 3600
    while data.get("status") not in ("succeeded", "failed", "canceled"):
        if time.time() > deadline:
            raise UserError("Replicate слишком долго стирает субтитры.")
        time.sleep(5)
        data = net.request("Replicate", "GET", data["urls"]["get"], headers=headers, timeout=60).json()
    if data["status"] != "succeeded":
        raise UserError(f"Replicate не смог стереть субтитры ({data.get('error')}). Попробуйте размытую полосу.")
    out = data.get("output")
    out = out[0] if isinstance(out, list) else out
    dst = d / "erased.mp4"
    dst.write_bytes(net.request("Replicate", "GET", out, timeout=600).content)
    try:  # the copy on Replicate is not needed any more
        fid = up.get("id")
        if fid:
            net.request("Replicate", "DELETE", f"https://api.replicate.com/v1/files/{fid}", headers=headers, tries=1)
    except UserError:
        pass
    return dst


def _picture(d: Path, meta: dict, src: Path, total: float) -> Path:
    """The new picture: subtitles hidden, optionally mirrored and slightly reframed, as long as the new voice."""
    o, box = meta["opts"], meta["box"]
    w, h = meta["width"], meta["height"]
    w2, h2 = w - w % 2, h - h % 2
    chain = [f"scale={w2}:{h2}"]
    if o["subs_mode"] == "blur":
        y, bh = int(h2 * box["y"] / 100) // 2 * 2, max(int(h2 * box["h"] / 100) // 2 * 2, 8)
        y = min(y, h2 - bh)
        r = max(2, min(bh // 2 - 1, 30))
        fc = (f"[0:v]{chain[0]},split[a][b];[b]crop={w2}:{bh}:0:{y},boxblur={r}:4,eq=brightness=-0.12[bl];"
              f"[a][bl]overlay=0:{y}")
    else:
        fc = f"[0:v]{chain[0]}"
    if o["restyle"]:
        # Mirrored, 6% closer and a touch more colour: the same story, a new-looking picture.
        fc += (f",hflip,scale={int(w2 * 1.06) // 2 * 2}:{int(h2 * 1.06) // 2 * 2},crop={w2}:{h2},"
               "eq=saturation=1.08:contrast=1.03")
    pad = max(0.0, total - meta["duration"])
    if pad > 0.05:
        fc += f",tpad=stop_mode=clone:stop_duration={pad:.2f}"
    fc += ",format=yuv420p[v]"
    out = d / "picture.mp4"
    media.run(["-i", src, "-filter_complex", fc, "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", 19, "-r", 30, out], "Обработка видео")
    return out


def _num(v, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(float(v), hi))
    except (TypeError, ValueError):
        return default


def subs_centre(meta: dict, opts: dict):
    """Where the middle of the new subtitles sits, % from the top: the owner's choice, else on the blurred band,
    else None (the usual place)."""
    if opts.get("subs_pos") is not None:
        return opts["subs_pos"]
    if opts.get("subs_mode", "blur") == "blur":
        box = meta["box"]
        return box["y"] + box["h"] / 2
    return None


def _ass_margin(meta: dict, size: int, centre: float) -> int:
    """MarginV of a one-line subtitle whose middle is at `centre` % from the top."""
    return max(10, int(meta["height"] * (1 - centre / 100) - size / 2))


def _work(d: Path):
    meta = _read(d / "meta.json")
    o = meta["opts"]
    settings = load_settings()
    settings["elevenlabs_voice_id"] = o["voice_id"]
    usage = {}
    vertical = meta["height"] > meta["width"]
    src = d / "source.mp4"

    _log(d, "Достаю звук из видео…")
    audio = d / "speech.mp3"
    media.run(["-i", src, "-vn", "-ac", 1, "-ar", 22050, "-b:a", "64k", audio], "Звук")
    tr = _read(d / "transcript.json")
    if not tr:
        _log(d, "Распознаю речь (ElevenLabs)…")
        tr = transcribe(settings, audio)
        _write(d / "transcript.json", tr)
    segs = segments(tr.get("words") or [])
    if not segs:
        raise UserError("В видео не нашлось речи.")
    _log(d, f"Речь распознана: {len(segs)} фраз. Перевожу на английский…")
    lines = _read(d / "lines.json")
    if not lines or len(lines) != len(segs):
        lines = translate(settings, segs, o["lang"], usage)
        _write(d / "lines.json", lines)
    text = " ".join(lines)
    md = _read(d / "seo.json")
    if not md:
        _log(d, "Пишу название, описание и теги под поиск…")
        md = metadata(settings, text, o["lang"], vertical, usage)
        _write(d / "seo.json", md)
    meta["title"] = md.get("title", "")
    meta["title_ru"] = md.get("title_ru", "")
    _write(d / "meta.json", meta)

    _log(d, f"Озвучиваю {len(segs)} фраз голосом {o['voice_name'] or o['voice_id']}…")
    scenes, durations, total = build_voice(d, settings, segs, lines, o["lang"], meta["duration"], usage)
    total = max(total, meta["duration"])
    durations[-1] += total - sum(durations)

    picture_src = src
    if o["subs_mode"] == "ai":
        _log(d, "Стираю старые субтитры нейросетью (Replicate), это может занять несколько минут…")
        picture_src = _replicate_erase(d, settings, src)
    _log(d, "Обрабатываю картинку" + (": закрываю старые субтитры" if o["subs_mode"] == "blur" else "")
         + (", отражаю и меняю кадр" if o["restyle"] else "") + "…")
    picture = _picture(d, meta, picture_src, total)

    track = musiclib.pick(md.get("music_style_ru", "")) if o["music"] == "library" else None
    _log(d, f"Фоновая музыка: {track.name}" if track else "Без фоновой музыки.")
    subs = None
    fdir = None
    w, h = meta["width"] - meta["width"] % 2, meta["height"] - meta["height"] % 2
    words = captions.timeline(d, "seg", scenes, durations)
    cues = captions.group(words, 3, 18) if vertical else captions.group(words, 8, 42)
    captions.write_srt(cues, d / "output" / "subtitles.srt")
    if o["new_subs"]:
        fdir, family = captions.font()
        size = round((96 if vertical else 60) * o.get("subs_scale", 1.0))
        centre = subs_centre(meta, o)
        margin = _ass_margin(meta, size, centre) if centre is not None else None
        subs = d / "subtitles.ass"
        captions.write_ass(cues, subs, w, h, vertical, family, margin_v=margin, size=size)
    _log(d, "Собираю видео…")
    s = settings
    media.final_mix(picture, d / "narration.wav", track, d / "output" / "video.mp4", float(s["music_volume"]),
                    float(s["loudness_lufs"]), total, subs=subs, fonts=fdir)
    frame = d / "frame.png"
    media.run(["-ss", min(total / 3, 5), "-i", d / "output" / "video.mp4", "-frames:v", 1, frame], "Кадр")
    thumb.render(frame, d / "output" / "thumbnail.jpg", md.get("thumbnail_text", ""), "left", vertical)
    (d / "output" / "youtube.txt").write_text(
        f"TITLE:\n{md.get('title', '')}\n\nDESCRIPTION:\n{md.get('description', '')}\n\nTAGS:\n"
        + ", ".join(md.get("tags") or []) + "\n\nTEXT:\n" + "\n".join(lines) + "\n", "utf-8")
    for tmp in ("picture.mp4", "erased.mp4", "frame.png", "speech.mp3"):
        (d / tmp).unlink(missing_ok=True)
    meta = _read(d / "meta.json")
    meta.update(usage=usage, result_duration=total)
    _write(d / "meta.json", meta)
    try:
        from . import exporter
        _log(d, f"Файлы лежат в папке: {exporter.export_remake(d.name, meta, d / 'output')}")
    except Exception as e:  # noqa: BLE001 - the video is ready anyway
        _log(d, f"Не удалось сложить файлы в отдельную папку: {e}")
    llm_cost = usage.get("llm_cost", 0)
    _log(d, f"Видео готово: {total:.0f} сек. Потрачено: текст ~${llm_cost:.2f} + "
         f"{int(usage.get('tts_chars', 0))} символов ElevenLabs + распознавание речи.")
