"""Lumean (lumean.app): ElevenLabs voices through a Russian reseller with a subscription.

How it works (https://api.lumean.app/docs/llm.md):
- the voice lives in a template (config.tts_settings.voice_id); one template "Авто ютуб" is made once and reused,
  the voice, model and language of each order go in config_override;
- POST /orders answers at once, the order is done in the background: GET /orders/{id} until it is completed;
- result.files[] holds the audio path, result.service_files[] the subtitles and result.json (word timings);
  a path becomes a one-hour download link through POST /storage/url.
Lumean cuts long text into pieces itself and stitches the voice, so a whole narration goes in one order.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from . import net
from .net import UserError

BASE = "https://api.lumean.app/api/public"
SERVICE = "Lumean"
TEMPLATE_NAME = "Авто ютуб"
MODELS = [
    ("eleven_multilingual_v2", "Multilingual v2 — стабильно, все настройки голоса работают (рекомендую)"),
    ("eleven_v3", "Eleven v3 — самый выразительный, скорость не меняется"),
    ("eleven_v4", "Eleven v4 — новейший"),
    ("eleven_turbo_v2_5", "Turbo v2.5 — быстро и дешевле"),
]
DONE = {"completed", "result_delivered"}
DEAD = {"failed", "compensated", "cancelled"}


def _key(settings) -> str:
    key = (settings.get("lumean_key") or "").strip()
    if not key:
        raise UserError("Не указан ключ Lumean. Вставьте его в Настройках → «2. Озвучка».")
    return key


def _call(settings, method: str, path: str, **kw):
    headers = {"X-API-KEY": _key(settings), "Accept": "application/json"}
    r = net.request(SERVICE, method, BASE + path, headers=headers, timeout=kw.pop("timeout", 120), **kw)
    try:
        data = r.json()
    except ValueError:
        raise UserError(f"Lumean ответил не тем, что ожидалось: {r.text[:200]}")
    if isinstance(data, dict) and data.get("success") is False:
        raise UserError(f"Lumean: {data.get('message') or data.get('reason') or data}"[:400])
    return data.get("data") if isinstance(data, dict) and "data" in data else data


def _explain(e: UserError) -> UserError:
    msg = str(e)
    if "(403)" in msg or "ошибка 403" in msg:
        return UserError("Lumean: у ключа не хватает прав. Создайте в кабинете Lumean (API → Ключи) новый ключ с "
                         "пресетом «full» (нужны права шаблонов и заказов) и вставьте его в Настройках.")
    return e


def account(settings) -> dict:
    """What the key can do and how much is left (shown in Settings)."""
    return _call(settings, "GET", "/key")


def voices(settings, language: str = "", search: str = "", gender: str = "", page: int = 1) -> dict:
    """ElevenLabs voice library through Lumean: [{voice_id, name, description, language, gender, preview_url}]."""
    params = {"provider": "elevenlabs", "page": max(1, int(page or 1)), "per_page": 30}
    if language:
        params["language"] = language
    if search:
        params["search"] = search
    if gender in ("male", "female"):
        params["gender"] = gender
    try:
        d = _call(settings, "GET", "/voices", params=params, timeout=60)
    except UserError as e:
        raise _explain(e)
    items = []
    for v in (d or {}).get("items") or []:
        items.append({"voice_id": v.get("voice_id"), "name": v.get("name") or v.get("voice_id"),
                      "description": v.get("description") or "", "language": v.get("language") or "",
                      "gender": v.get("gender") or "", "accent": v.get("accent") or "", "age": v.get("age") or "",
                      "preview_url": v.get("preview_url") or ""})
    return {"items": items, "page": d.get("page", page) if d else page, "has_more": bool((d or {}).get("has_more"))}


def _voice_settings(settings) -> dict:
    out = {"stability": float(settings.get("voice_stability") or 0.5),
           "similarity_boost": float(settings.get("voice_similarity") or 0.75),
           "style": float(settings.get("voice_style") or 0.0), "use_speaker_boost": True,
           "speed": min(1.2, max(0.7, float(settings.get("voice_speed") or 1.0)))}
    if (settings.get("lumean_model") or "").startswith("eleven_v3"):
        # v3 takes only 0, 0.5 or 1 for stability.
        out["stability"] = min((0.0, 0.5, 1.0), key=lambda x: abs(x - out["stability"]))
    return out


def _template(settings) -> str:
    """The id of our reusable template; made once per key and remembered in the settings."""
    from .config import save_settings
    key_mark = hashlib.sha256(_key(settings).encode()).hexdigest()[:10]
    saved = settings.get("lumean_template_id") or ""
    if saved.startswith(key_mark + ":"):
        return saved.split(":", 1)[1]
    try:
        found = _call(settings, "GET", "/templates", params={"page": 1, "per_page": 30, "search": TEMPLATE_NAME,
                                                                "service": "elevenlabs"})
        tid = next((t.get("id") for t in (found or {}).get("items") or [] if t.get("name") == TEMPLATE_NAME), None)
        if not tid:
            body = {"service_key": "elevenlabs", "name": TEMPLATE_NAME, "config": {"tts_settings": {
                "mode": "mode_v1", "model_id": "eleven_multilingual_v2",
                "voice_id": settings.get("elevenlabs_voice_id") or "JBFqnCBsd6RMkjVDRZzb",
                "advanced_voice_settings": True, "prev_next_text": True,
                "voice_settings": _voice_settings(settings)}}}
            tid = _call(settings, "POST", "/templates", json=body)["id"]
    except UserError as e:
        raise _explain(e)
    settings["lumean_template_id"] = f"{key_mark}:{tid}"
    save_settings({"lumean_template_id": settings["lumean_template_id"]})
    return tid


def _download(settings, path: str) -> bytes:
    url = _call(settings, "POST", "/storage/url", json={"path": path})["url"]
    return net.request(SERVICE, "GET", url, timeout=600).content


def _wait(settings, order_id: str, progress=None, limit: float = 90 * 60) -> dict:
    started, retried, delay = time.time(), 0, 3.0
    while True:
        order = _call(settings, "GET", f"/orders/{order_id}")
        status = order.get("status")
        if status in DONE:
            return order
        if status in DEAD:
            reason = ((order.get("result") or {}).get("user_message") or status)
            raise UserError(f"Lumean не смог озвучить текст: {reason}")
        if status == "partially_completed":
            if retried >= 2:
                bad = [i.get("failure_reason") for i in order.get("items") or []
                       if i.get("status") in ("failed", "policy_flagged") and i.get("failure_reason")]
                raise UserError("Lumean озвучил не весь текст: часть фрагментов не получилась"
                                + (f" ({bad[0]})" if bad else "") + ". Попробуйте ещё раз или поправьте текст.")
            retried += 1
            _call(settings, "POST", f"/orders/{order_id}/items/retry-failed")
        if progress:
            try:
                progress(float(order.get("progress_percent") or order.get("progress") or 0) / 100)
            except (TypeError, ValueError):
                pass
        if time.time() - started > limit:
            raise UserError("Lumean слишком долго озвучивает текст. Заказ остался в кабинете Lumean; "
                            "нажмите «Повторить» позже.")
        time.sleep(delay)
        delay = min(delay * 1.4, 15)


# ------------------------------------------------------------------ word timings

def _find_alignment(node):
    """The ElevenLabs-style alignment {characters, character_start_times_seconds, ...} anywhere in result.json."""
    if isinstance(node, dict):
        if isinstance(node.get("characters"), list) and isinstance(node.get("character_start_times_seconds"), list):
            return node
        for key in ("normalized_alignment", "alignment"):
            if isinstance(node.get(key), dict):
                found = _find_alignment(node[key])
                if found:
                    return found
        for value in node.values():
            found = _find_alignment(value)
            if found:
                return found
    elif isinstance(node, list):
        parts = [_find_alignment(x) for x in node]
        parts = [p for p in parts if p]
        if len(parts) > 1:
            # Several pieces, each timed from its own start: lay them one after another.
            chars, st, en, offset = [], [], [], 0.0
            for p in parts:
                ps, pe = p["character_start_times_seconds"], p.get("character_end_times_seconds") or p["character_start_times_seconds"]
                base = offset if ps and ps[0] < offset - 0.5 else 0.0
                chars += list(p["characters"]) + [" "]
                st += [x + base for x in ps] + [(pe[-1] + base) if pe else offset]
                en += [x + base for x in pe] + [(pe[-1] + base) if pe else offset]
                offset = en[-1] if en else offset
            return {"characters": chars, "character_start_times_seconds": st, "character_end_times_seconds": en}
        if parts:
            return parts[0]
    return None


def _srt_words(srt: str) -> list:
    """Word timings from subtitles: each cue's time is shared by its words in proportion to their length."""
    out = []
    for block in re.split(r"\n\s*\n", srt.replace("\r", "")):
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", block)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        a = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        b = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        text = " ".join(line for line in block[m.end():].split("\n") if line.strip())
        tokens = text.split()
        total = sum(len(t) + 1 for t in tokens) or 1
        t = a
        for tok in tokens:
            d = (b - a) * (len(tok) + 1) / total
            out.append((tok, t, t + d))
            t += d
    return out


def _words_to_alignment(words: list) -> dict:
    chars, st, en = [], [], []
    for i, (word, a, b) in enumerate(words):
        if i:
            chars.append(" "); st.append(a); en.append(a)
        step = (b - a) / max(len(word), 1)
        for k, ch in enumerate(word):
            chars.append(ch); st.append(a + k * step); en.append(a + (k + 1) * step)
    return {"characters": chars, "character_start_times_seconds": st, "character_end_times_seconds": en}


def synthesize(settings, text: str, out_path: Path, language: str = "", progress=None) -> int:
    """Voices the whole text into out_path (mp3) and writes word timings next to it (.json, ElevenLabs format)."""
    text = (text or "").strip()
    if not text:
        raise UserError("Пустой текст для озвучки.")
    try:
        template = _template(settings)
        tts = {"voice_id": settings.get("elevenlabs_voice_id") or "JBFqnCBsd6RMkjVDRZzb",
               "model_id": settings.get("lumean_model") or "eleven_multilingual_v2",
               "advanced_voice_settings": True, "voice_settings": _voice_settings(settings)}
        if language:
            tts["language_code"] = language
        body = {"template_id": template, "input_text": text, "name": f"Авто ютуб {out_path.stem}"[:120],
                "config_override": {"tts_settings": tts}}
        order = _call(settings, "POST", "/orders", json=body, tries=1, timeout=300)
        order = _wait(settings, order["id"], progress)
    except UserError as e:
        raise _explain(e)
    result = order.get("result") or {}
    files = [p for p in result.get("files") or [] if isinstance(p, str)]
    audio_path = next((p for p in files if re.search(r"\.(mp3|wav|ogg|m4a)$", p, re.I)), files[0] if files else None)
    if not audio_path:
        raise UserError("Lumean закончил заказ, но не отдал файл озвучки.")
    audio = _download(settings, audio_path)
    if len(audio) < 500:
        raise UserError("Lumean отдал пустой файл озвучки.")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(audio)
    align_path = out_path.with_suffix(".json")
    align_path.unlink(missing_ok=True)
    service = [p for p in result.get("service_files") or [] if isinstance(p, str)]
    alignment = None
    rich = next((p for p in service if p.endswith("result.json")), None)
    if rich:
        try:
            raw = _download(settings, rich)
            out_path.with_suffix(".lumean.json").write_bytes(raw)  # kept for diagnosis
            alignment = _find_alignment(json.loads(raw))
        except (UserError, ValueError):
            alignment = None
    if not alignment:
        srt = next((p for p in service if p.endswith(".srt")), None)
        if srt:
            try:
                words = _srt_words(_download(settings, srt).decode("utf-8", "replace"))
                alignment = _words_to_alignment(words) if words else None
            except UserError:
                alignment = None
    if alignment and alignment.get("characters"):
        align_path.write_text(json.dumps(alignment), encoding="utf-8")
    return len(text)
