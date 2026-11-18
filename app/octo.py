"""Octo Browser: the owner's browser profiles, each signed in to one YouTube channel.

- The list of profiles with their names comes from the cloud API (https://app.octobrowser.net/api/v2/automation,
  header X-Octo-Api-Token; the token is in the master account settings, tab «Дополнительно»).
- Starting and stopping goes through the local API of the running Octo Browser app (http://localhost:58888).
  A profile started with debug_port gets a ws_endpoint: the uploader drives that browser through it (CDP).
A profile the owner opened by hand has no debug port: it is closed and started again through the API.

Without the token: the local API needs none. The owner opens each channel's profile in Octo by hand once and
presses «Найти открытые профили»: their UUIDs are read from /api/profiles/active and remembered (settings
"octo_local_profiles", the owner gives each a name). A UUID can also be pasted by hand from the profile card.
"""
from __future__ import annotations

import time

import httpx

from . import net
from .net import UserError

CLOUD = "https://app.octobrowser.net/api/v2/automation"
LOCAL = "http://localhost:58888"
SERVICE = "Octo Browser"


def _token(settings) -> str:
    token = (settings.get("octo_token") or "").strip()
    if not token:
        raise UserError("Не указан API-токен Octo Browser. Он лежит в Octo: настройки мастер-аккаунта → вкладка "
                        "«Дополнительно». Вставьте его в «Сборке из папок» → «Автовыкладка».")
    return token


def local_profiles(settings) -> list[dict]:
    """Profiles remembered without the token: [{uuid, title, tags, local}]."""
    out = []
    for p in settings.get("octo_local_profiles") or []:
        if isinstance(p, dict) and p.get("uuid"):
            out.append({"uuid": p["uuid"], "title": p.get("title") or p["uuid"][:8], "tags": [], "local": True})
    return out


def profiles(settings) -> list[dict]:
    """[{uuid, title, tags}] of all profiles, sorted by title. No token: the profiles remembered on this Mac."""
    if not (settings.get("octo_token") or "").strip():
        return sorted(local_profiles(settings), key=lambda p: str(p["title"]).lower())
    out, page = [], 0
    while True:
        r = net.request(SERVICE, "GET", f"{CLOUD}/profiles", headers={"X-Octo-Api-Token": _token(settings)},
                        params={"page": page, "page_len": 100, "fields": "title,tags"}, timeout=60, tries=3)
        d = r.json()
        items = d.get("data") or []
        out += [{"uuid": p.get("uuid"), "title": p.get("title") or p.get("uuid"), "tags": p.get("tags") or []}
                for p in items if p.get("uuid")]
        if len(items) < 100 or len(out) >= int(d.get("total_count") or 0):
            break
        page += 1
    known = {p["uuid"] for p in out}
    out += [p for p in local_profiles(settings) if p["uuid"] not in known]
    return sorted(out, key=lambda p: str(p["title"]).lower())


def scan_open() -> list[dict]:
    """Profiles open in Octo right now: [{uuid, title}] (title only if Octo gives one)."""
    out = []
    for p in active():
        uuid = p.get("uuid") or p.get("profile_uuid")
        if uuid:
            out.append({"uuid": uuid, "title": p.get("title") or p.get("name") or ""})
    return out


def _local(method: str, path: str, **kw):
    try:
        r = httpx.request(method, LOCAL + path, timeout=kw.pop("timeout", 120), **kw)
    except httpx.TransportError:
        raise UserError("Octo Browser не запущен на этом Mac. Откройте программу Octo Browser и войдите в аккаунт, "
                        "потом нажмите «Повторить».")
    if r.status_code >= 400:
        hint = ""
        if r.status_code in (401, 402, 403):
            hint = (" Похоже, тариф Octo не даёт доступа к локальному API (автоматизации). Проверьте тариф или "
                    "войдите в Octo мастер-аккаунтом.")
        elif r.status_code == 404 and "/start" in path:
            hint = " Профиль с таким UUID не найден в Octo: проверьте UUID или найдите профиль заново."
        raise UserError(f"Octo Browser: ошибка {r.status_code}.{hint} Ответ: {r.text[:300]}")
    try:
        return r.json()
    except ValueError:
        return {}


def active() -> list[dict]:
    d = _local("GET", "/api/profiles/active", timeout=20)
    return d if isinstance(d, list) else []


def stop(uuid: str):
    try:
        _local("POST", "/api/profiles/stop", json={"uuid": uuid}, timeout=60)
    except UserError:
        pass


def start(uuid: str) -> str:
    """Starts the profile with a debug port (or reuses it if already started that way); returns the ws endpoint."""
    for p in active():
        if p.get("uuid") == uuid:
            if p.get("ws_endpoint"):
                return p["ws_endpoint"]
            stop(uuid)  # opened by hand: no way to drive it, so it is opened again through the API
            for _ in range(30):
                if not any(x.get("uuid") == uuid for x in active()):
                    break
                time.sleep(1)
    d = _local("POST", "/api/profiles/start", json={"uuid": uuid, "headless": False, "debug_port": True,
                                                    "timeout": 120, "only_local": True}, timeout=180)
    ws = d.get("ws_endpoint")
    if not ws:
        raise UserError(f"Octo Browser запустил профиль, но не дал доступ к нему: {str(d)[:300]}")
    return ws
