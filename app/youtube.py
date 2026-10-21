"""YouTube channels: Google sign-in (OAuth, desktop app flow), uploads and thumbnails.
Several channels can be connected; each one keeps its own token and its own profile
(niche, voice, image style, privacy, auto-upload)."""
import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
from pathlib import Path

from . import net
from .config import DATA_DIR
from .net import UserError

YT_DIR = DATA_DIR / "youtube"
CLIENT_FILE = YT_DIR / "client_secret.json"
CH_DIR = YT_DIR / "channels"
CH_DIR.mkdir(parents=True, exist_ok=True)

SCOPES = ("https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly "
          "https://www.googleapis.com/auth/yt-analytics.readonly")
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_API = "https://www.googleapis.com/upload/youtube/v3"

CATEGORIES = [("27", "Образование"), ("24", "Развлечения"), ("22", "Люди и блоги"), ("1", "Фильмы и анимация"),
              ("19", "Путешествия и события"), ("28", "Наука и техника")]

PROFILE_DEFAULTS = {
    "niche": "",            # empty = use the general setting
    "voice_id": "", "voice_name": "",
    "image_style": "",
    "privacy": "private",   # private | unlisted | public
    "category": "27",
    "auto_upload": False,
    "synthetic": True,      # tell YouTube the video contains AI-generated content
}

_lock = threading.Lock()
_pending = {}  # state -> (code_verifier, redirect_uri)
last_error = ""


def _write(path: Path, data: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


# ---------------------------------------------------------------- client secret

def save_client(raw: bytes):
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise UserError("Это не тот файл. Нужен JSON-файл, скачанный в Google Cloud (кнопка «Download JSON»).")
    if "web" in data:
        raise UserError("Этот ключ сделан для типа «Web application». Создайте новый OAuth-клиент с типом "
                        "«Desktop app» («Приложение для компьютера») и загрузите его файл.")
    inst = data.get("installed")
    if not inst or not inst.get("client_id") or not inst.get("client_secret"):
        raise UserError("В файле нет client_id и client_secret. Скачайте файл OAuth-клиента типа «Desktop app».")
    YT_DIR.mkdir(parents=True, exist_ok=True)
    _write(CLIENT_FILE, data)


def _client() -> dict:
    if not CLIENT_FILE.exists():
        raise UserError("Сначала загрузите файл ключа Google (client_secret.json) в Настройках, раздел «YouTube».")
    return json.loads(CLIENT_FILE.read_text("utf-8"))["installed"]


def has_client() -> bool:
    return CLIENT_FILE.exists()


# ---------------------------------------------------------------- sign-in

def auth_url(redirect_uri: str) -> str:
    c = _client()
    state = secrets.token_urlsafe(16)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    _pending[state] = (verifier, redirect_uri)
    q = {
        "client_id": c["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
        "scope": SCOPES, "access_type": "offline", "prompt": "consent select_account",
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(q)


def finish(code: str, state: str) -> dict:
    if state not in _pending:
        raise UserError("Вход устарел. Нажмите «Подключить канал» ещё раз.")
    verifier, redirect_uri = _pending.pop(state)
    c = _client()
    r = net.request("Google", "POST", TOKEN_URL, tries=2, data={
        "code": code, "client_id": c["client_id"], "client_secret": c["client_secret"],
        "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier,
    })
    tok = r.json()
    if not tok.get("refresh_token"):
        raise UserError("Google не выдал постоянный доступ. Попробуйте подключить канал ещё раз.")
    access = tok["access_token"]
    info = net.request("YouTube", "GET", f"{API}/channels", tries=2, params={"part": "snippet", "mine": "true"},
                       headers={"Authorization": f"Bearer {access}"}).json()
    items = info.get("items") or []
    if not items:
        raise UserError("У выбранного Google-аккаунта нет канала YouTube. Создайте канал или выберите другой аккаунт.")
    ch = items[0]
    path = CH_DIR / f"{ch['id']}.json"
    old = json.loads(path.read_text("utf-8")) if path.exists() else {}
    data = {
        "id": ch["id"],
        "title": ch["snippet"].get("title", ""),
        "thumb": ((ch["snippet"].get("thumbnails") or {}).get("default") or {}).get("url", ""),
        "refresh_token": tok["refresh_token"],
        "access_token": access,
        "expires_at": time.time() + int(tok.get("expires_in", 3600)) - 60,
        "profile": {**PROFILE_DEFAULTS, **(old.get("profile") or {})},
        "connected_at": time.time(),
    }
    _write(path, data)
    return data


# ---------------------------------------------------------------- channels

def _load(cid: str) -> dict:
    p = CH_DIR / f"{Path(cid).name}.json"
    if not p.exists():
        raise UserError("Канал не найден. Возможно, его отключили в Настройках.")
    return json.loads(p.read_text("utf-8"))


def list_channels() -> list:
    out = []
    for p in sorted(CH_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text("utf-8"))
        except Exception:
            continue
        out.append({"id": d["id"], "title": d["title"], "thumb": d.get("thumb", ""),
                    "profile": {**PROFILE_DEFAULTS, **(d.get("profile") or {})},
                    "needs_login": bool(d.get("needs_login"))})
    return out


def get_profile(cid: str) -> dict:
    try:
        return {**PROFILE_DEFAULTS, **(_load(cid).get("profile") or {})}
    except UserError:
        return {}


def save_profile(cid: str, profile: dict):
    with _lock:
        d = _load(cid)
        p = {**PROFILE_DEFAULTS, **(d.get("profile") or {})}
        for k in PROFILE_DEFAULTS:
            if k in profile:
                p[k] = bool(profile[k]) if isinstance(PROFILE_DEFAULTS[k], bool) else str(profile[k] or "").strip()
        d["profile"] = p
        _write(CH_DIR / f"{d['id']}.json", d)


def remove(cid: str):
    (CH_DIR / f"{Path(cid).name}.json").unlink(missing_ok=True)


def _token(cid: str) -> str:
    with _lock:
        d = _load(cid)
        if d.get("access_token") and d.get("expires_at", 0) > time.time():
            return d["access_token"]
        c = _client()
        import httpx
        r = httpx.post(TOKEN_URL, timeout=60, data={
            "client_id": c["client_id"], "client_secret": c["client_secret"],
            "refresh_token": d["refresh_token"], "grant_type": "refresh_token"})
        if r.status_code >= 400:
            if "invalid_grant" in r.text:
                d["needs_login"] = True
                _write(CH_DIR / f"{d['id']}.json", d)
                raise UserError(f"Доступ к каналу «{d['title']}» закончился. Откройте Настройки → YouTube и "
                                "нажмите «Подключить канал» для этого канала ещё раз. Чтобы это не повторялось "
                                "каждую неделю, переведите приложение в Google Cloud в режим «In production» "
                                "(см. инструкцию в Настройках).")
            raise UserError(net.explain_status("Google", r.status_code, r.text))
        tok = r.json()
        d["access_token"] = tok["access_token"]
        d["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
        d.pop("needs_login", None)
        _write(CH_DIR / f"{d['id']}.json", d)
        return d["access_token"]


# ---------------------------------------------------------------- upload

def _clean_title(t: str) -> str:
    t = " ".join((t or "").replace("<", "").replace(">", "").split())
    return t[:100] or "Untitled"


def _clean_tags(tags: list) -> list:
    out, total = [], 0
    for t in tags or []:
        t = " ".join(str(t).replace("<", "").replace(">", "").replace(",", " ").split())
        if not t:
            continue
        cost = len(t) + (2 if " " in t else 0) + 1
        if total + cost > 480:
            break
        out.append(t)
        total += cost
    return out


def upload(cid: str, video: Path, title: str, description: str, tags: list, privacy: str, publish_at: str,
           category: str, synthetic: bool, progress=None, language: str = "en", for_kids: bool = False) -> str:
    """Resumable upload. Returns the video id."""
    status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": bool(for_kids), "containsSyntheticMedia": bool(synthetic)}
    if publish_at:
        status["privacyStatus"] = "private"  # scheduled videos must be private until the publish time
        status["publishAt"] = publish_at
    body = {
        "snippet": {"title": _clean_title(title), "description": (description or "").replace("<", "").replace(">", "")[:4900],
                    "tags": _clean_tags(tags), "categoryId": category or "27",
                    "defaultLanguage": language, "defaultAudioLanguage": language},
        "status": status,
    }
    size = video.stat().st_size
    r = net.request("YouTube", "POST", f"{UPLOAD_API}/videos", tries=3,
                    params={"uploadType": "resumable", "part": "snippet,status"},
                    headers={"Authorization": f"Bearer {_token(cid)}", "X-Upload-Content-Type": "video/mp4",
                             "X-Upload-Content-Length": str(size), "Content-Type": "application/json; charset=UTF-8"},
                    json=body)
    session = r.headers.get("Location")
    if not session:
        raise UserError("YouTube не начал загрузку. Попробуйте ещё раз.")
    import httpx
    chunk = 16 * 1024 * 1024
    offset = 0
    fails = 0
    with open(video, "rb") as f:
        while offset < size:
            f.seek(offset)
            data = f.read(chunk)
            end = offset + len(data) - 1
            try:
                resp = httpx.put(session, content=data, timeout=600, headers={
                    "Authorization": f"Bearer {_token(cid)}", "Content-Length": str(len(data)),
                    "Content-Range": f"bytes {offset}-{end}/{size}"})
            except httpx.TransportError:
                resp = None
            if resp is not None and resp.status_code in (200, 201):
                if progress:
                    progress(1.0)
                return resp.json()["id"]
            if resp is not None and resp.status_code == 308:
                rng = resp.headers.get("Range")
                offset = int(rng.split("-")[1]) + 1 if rng else 0
                fails = 0
                if progress:
                    progress(offset / size)
                continue
            if resp is not None and resp.status_code < 500 and resp.status_code != 429:
                text = resp.text
                if "uploadLimitExceeded" in text or "quotaExceeded" in text:
                    raise UserError("YouTube: на сегодня достигнут лимит загрузок. Попробуйте завтра.")
                raise UserError(net.explain_status("YouTube", resp.status_code, text))
            # Network problem or server error: ask YouTube how much it already has, then continue.
            fails += 1
            if fails > 6:
                raise UserError("YouTube: загрузка много раз обрывалась. Проверьте интернет и нажмите «Загрузить» ещё раз.")
            time.sleep(min(60, 2 ** fails))
            try:
                st = httpx.put(session, timeout=60, headers={"Authorization": f"Bearer {_token(cid)}",
                                                             "Content-Range": f"bytes */{size}"})
                if st.status_code in (200, 201):
                    return st.json()["id"]
                rng = st.headers.get("Range")
                offset = int(rng.split("-")[1]) + 1 if rng else 0
            except httpx.TransportError:
                pass
    raise UserError("YouTube не подтвердил загрузку.")


def set_thumbnail(cid: str, video_id: str, jpg: Path):
    net.request("YouTube", "POST", f"{UPLOAD_API}/thumbnails/set", tries=3,
                params={"videoId": video_id, "uploadType": "media"},
                headers={"Authorization": f"Bearer {_token(cid)}", "Content-Type": "image/jpeg"},
                content=jpg.read_bytes())
