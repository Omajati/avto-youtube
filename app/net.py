"""HTTP helper with retries and human-readable (Russian) errors."""
import time

import httpx


class UserError(Exception):
    """Error with a message the user can understand."""


class Cancelled(Exception):
    pass


def explain_status(service: str, status: int, body: str) -> str:
    short = (body or "")[:300]
    if "missing_permission" in (body or ""):
        import re
        m = re.search(r"permission ([a-z_]+)", body or "")
        perm = m.group(1) if m else ""
        return (f"{service}: у ключа API не включено нужное разрешение{f' «{perm}»' if perm else ''}. "
                "Откройте настройки ключа на сайте сервиса и включите его (или создайте новый ключ без ограничений), "
                "затем вставьте ключ в Настройках ещё раз.")
    low = short.lower()
    if "api_key_id_used_as_api_key" in low or "invalid_api_key" in low:
        return (f"{service}: в Настройках вставлен не тот ключ. Похоже, это «ID ключа», а нужен сам ключ: он начинается "
                "с sk_ и показывается один раз, когда ключ создают. Создайте на сайте новый ключ, сразу скопируйте его "
                "и вставьте в Настройках.")
    if status == 402 or any(w in low for w in ("top_up", "exhausted balance", "insufficient", "quota", "credit")):
        return f"{service}: закончились деньги на аккаунте (ошибка {status}). Пополните баланс на сайте сервиса. Ответ: {short}"
    if status in (401, 403):
        return f"{service}: ключ API не подошёл (ошибка {status}). Проверьте ключ в Настройках. Ответ сервиса: {short}"
    if status == 402 or "quota" in short.lower() or "insufficient" in short.lower() or "credit" in short.lower():
        return f"{service}: закончились деньги или лимит на аккаунте (ошибка {status}). Пополните баланс. Ответ: {short}"
    if status == 429:
        return f"{service}: слишком много запросов, сервис просит подождать (429). Ответ: {short}"
    if status == 404:
        return f"{service}: модель или адрес не найдены (404). Проверьте название модели в Настройках. Ответ: {short}"
    if status == 422 or status == 400:
        return f"{service}: сервис отклонил запрос ({status}). Ответ: {short}"
    return f"{service}: ошибка сервиса {status}. Ответ: {short}"


RETRY_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529}


def request(service: str, method: str, url: str, *, tries: int = 5, timeout: float = 180, **kw) -> httpx.Response:
    delay = 3.0
    last = None
    for attempt in range(tries):
        try:
            r = httpx.request(method, url, timeout=httpx.Timeout(timeout, connect=min(20, timeout)),
                              follow_redirects=True, **kw)
        except httpx.TransportError as e:
            last = f"{service}: нет связи с сервисом ({e.__class__.__name__}). Проверьте интернет и VPN."
            time.sleep(delay)
            delay *= 2
            continue
        if r.status_code < 400:
            return r
        last = explain_status(service, r.status_code, r.text)
        if r.status_code in RETRY_STATUSES and attempt < tries - 1:
            ra = r.headers.get("retry-after")
            try:
                wait = float(ra) if ra else delay
            except ValueError:
                wait = delay
            time.sleep(min(wait, 60))
            delay *= 2
            continue
        raise UserError(last)
    raise UserError(last or f"{service}: не удалось выполнить запрос")
