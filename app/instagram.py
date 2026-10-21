"""Instagram Reels: publishing finished vertical videos straight to Instagram (Facebook Login for Business,
resumable upload: the file goes from this computer to Meta, no other service in between)."""
import re
import time
from pathlib import Path

from . import net
from .config import load_settings, save_settings
from .net import UserError

GRAPH = "https://graph.facebook.com/v26.0"
RUPLOAD = "https://rupload.facebook.com/ig-api-upload/v26.0"


def connect(app_id: str, app_secret: str, user_token: str) -> dict:
    """Turns a short user token from the Graph API Explorer into a lasting one and finds the Instagram account."""
    app_id, app_secret, user_token = (app_id or "").strip(), (app_secret or "").strip(), (user_token or "").strip()
    if not (app_id and app_secret and user_token):
        raise UserError("Заполните все три поля: App ID, App Secret и токен из Graph API Explorer.")
    try:
        r = net.request("Instagram", "GET", f"{GRAPH}/oauth/access_token", tries=2, timeout=60, params={
            "grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": app_secret,
            "fb_exchange_token": user_token})
        long_user = r.json().get("access_token") or user_token
    except UserError as e:
        raise UserError("Meta не принял данные. Проверьте App ID и App Secret (кабинет приложения → App settings → "
                        f"Basic) и сгенерируйте токен заново. Ответ: {e}")
    pages = net.request("Instagram", "GET", f"{GRAPH}/me/accounts", tries=2, timeout=60, params={
        "access_token": long_user, "fields": "name,access_token,instagram_business_account{id,username}"}).json()
    for p in pages.get("data") or []:
        ig = p.get("instagram_business_account") or {}
        if ig.get("id"):
            # A page token made from a long-lived user token does not expire.
            save_settings({"ig_token": p.get("access_token") or long_user, "ig_user_id": ig["id"],
                           "ig_username": ig.get("username", ""), "ig_page": p.get("name", ""),
                           "ig_token_time": time.time()})
            return {"username": ig.get("username", ""), "page": p.get("name", "")}
    raise UserError("Не нашёл Instagram-аккаунт. Проверьте, что аккаунт Instagram профессиональный и привязан к "
                    "странице Facebook, и что при создании токена вы отметили эту страницу и все разрешения из инструкции.")


def _auth(s=None):
    s = s or load_settings()
    if not s.get("ig_token") or not s.get("ig_user_id"):
        raise UserError("Instagram не подключён. Откройте Настройки → Instagram.")
    return s["ig_token"], s["ig_user_id"]


def caption(extras: dict) -> str:
    title = (extras.get("youtube_title") or "").strip()
    desc = (extras.get("description") or "").strip().split("\n\n")[0]
    tags = []
    for t in extras.get("tags") or []:
        t = re.sub(r"[^\w]", "", str(t))
        if t and t.lower() not in [x.lower() for x in tags]:
            tags.append(t)
    text = "\n\n".join(x for x in (title, desc) if x)
    if tags:
        text += "\n\n" + " ".join("#" + t for t in tags[:8])
    return text[:2200]


def publish(video: Path, text: str, say=None) -> dict:
    """Publishes a Reel. Returns {"media_id", "url"}."""
    s = load_settings()
    token, uid = _auth(s)
    say = say or (lambda msg: None)
    params = {"media_type": "REELS", "upload_type": "resumable", "caption": text, "share_to_feed": "true",
              "access_token": token}
    if s.get("ig_ai_label", True):
        params["is_ai_generated"] = "true"
    r = net.request("Instagram", "POST", f"{GRAPH}/{uid}/media", data=params, tries=2, timeout=120)
    cid = r.json().get("id")
    if not cid:
        raise UserError(f"Instagram не создал рилс: {r.text[:300]}")
    say("загружаю видео")
    data = video.read_bytes()
    up = net.request("Instagram", "POST", f"{RUPLOAD}/{cid}", content=data, tries=3, timeout=900, headers={
        "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(len(data))})
    if not (up.json() or {}).get("success"):
        raise UserError(f"Instagram не принял файл: {up.text[:300]}")
    say("Instagram обрабатывает видео")
    deadline = time.time() + 15 * 60
    while True:
        time.sleep(15)
        st = net.request("Instagram", "GET", f"{GRAPH}/{cid}", tries=3, timeout=60,
                         params={"fields": "status_code,status", "access_token": token}).json()
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise UserError(f"Instagram не смог обработать видео: {st.get('status') or code}")
        if time.time() > deadline:
            raise UserError("Instagram слишком долго обрабатывает видео. Попробуйте ещё раз позже.")
    say("публикую")
    r = net.request("Instagram", "POST", f"{GRAPH}/{uid}/media_publish", tries=2, timeout=120,
                    data={"creation_id": cid, "access_token": token})
    mid = r.json().get("id")
    if not mid:
        raise UserError(f"Instagram не опубликовал рилс: {r.text[:300]}")
    url = ""
    try:
        url = net.request("Instagram", "GET", f"{GRAPH}/{mid}", tries=2, timeout=60,
                          params={"fields": "permalink", "access_token": token}).json().get("permalink", "")
    except UserError:
        pass
    return {"media_id": mid, "url": url or f"https://www.instagram.com/{s.get('ig_username', '')}"}
