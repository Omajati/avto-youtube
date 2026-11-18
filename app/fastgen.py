"""Fast Gen (fast-gen.ai): Nano Banana, GPT Image and others by subscription (credits per hour, threads at once).

API (https://api.fast-gen.ai/openapi.json): POST /api/v6/generations {operation, prompt, aspect_ratio, inputs}
answers at once with an id; GET /api/v6/generations/{id} until succeeded; the result has a download_url or
inline data. Reference pictures go in `inputs` as data URIs (up to 5 MB each, up to 10 for Nano Banana).
When the hourly credits run out the service answers 429: we wait and go on instead of failing the whole video.
"""
from __future__ import annotations

import base64
import time

import httpx

from . import net
from .net import UserError

BASE = "https://api.fast-gen.ai"
SERVICE = "Fast Gen"
# Operations that take reference pictures (the others only draw from text).
REFERENCE_OPS = {"gemini_nano_banana_2_image_generate", "nano_banana_2_image_generate",
                 "nano_banana_2_lite_image_generate", "nano_banana_pro_image_generate", "openai_image_generate"}
RATIOS = {"16:9", "9:16", "1:1", "4:3", "3:4"}


def _key(settings) -> str:
    key = (settings.get("fastgen_key") or "").strip()
    if not key:
        raise UserError("Не указан ключ Fast Gen. Вставьте его в Настройках, раздел «Картинки».")
    return key


def _limit_hit(status: int, body: str) -> bool:
    """The plan's hourly credits or its threads are used up (429, or a 402/403 that says so)."""
    low = (body or "").lower()
    return status == 429 or (status in (402, 403) and any(w in low for w in ("limit", "quota", "credit", "concurren", "thread")))


def _send(settings, method: str, path: str, wait_limit: float = 3 * 3600, **kw) -> dict:
    """One request. When the hourly credits or the threads run out, the request waits for the window to free up
    and goes on (credits come back within the hour), so a long build is never lost to the limit."""
    headers = {"X-API-Key": _key(settings), "Accept": "application/json"}
    started, delay = time.time(), 5.0
    while True:
        try:
            r = httpx.request(method, BASE + path, headers=headers, timeout=httpx.Timeout(180, connect=20), **kw)
        except httpx.TransportError as e:
            if time.time() - started > 10 * 60:
                raise UserError(f"{SERVICE}: нет связи с сервисом ({e.__class__.__name__}). Проверьте интернет.")
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        if _limit_hit(r.status_code, r.text) or r.status_code in (502, 503, 504):
            if time.time() - started > wait_limit:
                raise UserError(f"{SERVICE}: лимит кредитов в час исчерпан и не восстановился за "
                                f"{int(wait_limit // 60)} мин. Нажмите «Повторить» позже. Ответ: {r.text[:200]}")
            try:
                pause = float(r.headers.get("retry-after") or delay)
            except ValueError:
                pause = delay
            time.sleep(min(max(pause, 5), 120))
            delay = min(delay * 1.5, 120)
            continue
        if r.status_code >= 400:
            raise UserError(net.explain_status(SERVICE, r.status_code, r.text))
        try:
            return r.json()
        except ValueError:
            raise UserError(f"{SERVICE} ответил не тем, что ожидалось: {r.text[:200]}")


def usage(settings) -> dict:
    """Plan limits and what is used this hour (for Settings)."""
    return _send(settings, "GET", "/api/v6/usage", wait_limit=30)


_threads_cache: dict = {"key": "", "at": 0.0, "value": None}


def _find_threads(data, path: str = ""):
    """Numbers whose key speaks of threads / concurrency, with how sure we are that it is the plan's limit."""
    out = []
    if isinstance(data, dict):
        for k, v in data.items():
            name = f"{path}.{k}".lower()
            if isinstance(v, (dict, list)):
                out += _find_threads(v, name)
            elif isinstance(v, (int, float)) and not isinstance(v, bool) and 1 <= v <= 500 and \
                    any(w in name for w in ("thread", "concurren", "parallel", "simultan")):
                used = any(w in name for w in ("used", "active", "running", "current", "busy", "in_use"))
                limit = any(w in name for w in ("limit", "max", "total", "allowed", "plan"))
                out.append((0 if used else 2 if limit else 1, int(v)))
    elif isinstance(data, list):
        for item in data:
            out += _find_threads(item, path)
    return out


def plan_threads(settings) -> int | None:
    """How many pictures the plan draws at once (from /usage), cached for 10 minutes; None if unknown."""
    key = (settings.get("fastgen_key") or "").strip()
    if not key:
        return None
    cache = _threads_cache
    if cache["key"] == key and time.time() - cache["at"] < 600:
        return cache["value"]
    value = None
    try:
        found = [x for x in _find_threads(usage(settings)) if x[0] > 0]
        if found:
            best = max(rank for rank, _ in found)
            value = max(n for rank, n in found if rank == best)
    except Exception:  # noqa: BLE001 - unknown limits fall back to the default
        value = None
    cache.update(key=key, at=time.time(), value=value)
    return value


def generate(settings, prompt: str, aspect: str = "16:9", refs: list | None = None) -> bytes:
    """Image bytes. refs: data URIs of reference pictures (characters, places, items)."""
    op = settings.get("fastgen_model") or "gemini_nano_banana_2_image_generate"
    if aspect not in RATIOS:
        aspect = "16:9"
    body = {"operation": op, "prompt": prompt, "aspect_ratio": aspect}
    if refs and op in REFERENCE_OPS:
        body["inputs"] = [{"filename": f"reference_{i + 1}.jpg", "input": u} for i, u in enumerate(refs[:10])]
    started = time.time()
    job = _send(settings, "POST", "/api/v6/generations", json=body)
    gid = job.get("id")
    if not gid:
        raise UserError(f"{SERVICE} не принял задание: {str(job)[:300]}")
    delay = 2.0
    while True:
        time.sleep(delay)
        delay = min(delay * 1.3, 4)
        st = _send(settings, "GET", f"/api/v6/generations/{gid}")
        status = st.get("status")
        if status == "succeeded":
            break
        if status == "failed":
            msg = st.get("error") or st.get("error_code") or "без объяснения"
            raise UserError(f"{SERVICE} не нарисовал картинку: {msg}"[:400])
        if time.time() - started > 15 * 60:
            try:
                _send(settings, "DELETE", f"/api/v6/generations/{gid}", wait_limit=10)
            except UserError:
                pass
            raise UserError(f"{SERVICE}: картинка рисуется дольше 15 минут, задание отменено.")
    for item in st.get("results") or []:
        if item.get("type") not in (None, "image"):
            continue
        if item.get("download_url"):
            return net.request(SERVICE, "GET", item["download_url"], timeout=180).content
        if (item.get("data") or "").startswith("data:"):
            return base64.b64decode(item["data"].split(",", 1)[1])
    raise UserError(f"{SERVICE} закончил задание, но не отдал картинку.")
