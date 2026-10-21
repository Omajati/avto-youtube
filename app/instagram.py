"""Instagram Reels: publishing finished vertical videos straight to Instagram (Facebook Login for Business,
resumable upload: the file goes from this computer to Meta, no other service in between)."""
import re
import time
from pathlib import Path

from . import net
from .config import load_settings, save_settings
from .net import UserError

GRAPH = "https://graph.facebook.com/v26.0"
IG_GRAPH = "https://graph.instagram.com"  # accounts connected through «API setup with Instagram login»
# Publishing needs a versioned address there: without a version Instagram ignores upload_type=resumable.
IG_VERSION = "v24.0"
RUPLOAD = "https://rupload.facebook.com/ig-api-upload/v26.0"


def connect(app_id: str, app_secret: str, user_token: str) -> dict:
    """Turns a short user token from the Graph API Explorer into a lasting one and finds the Instagram account."""
    app_id, app_secret, user_token = (app_id or "").strip(), (app_secret or "").strip(), (user_token or "").strip()
    if user_token.startswith("IG"):
        return _connect_ig(app_secret, user_token)
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
                           "ig_login": "", "ig_token_time": time.time()})
            return {"username": ig.get("username", ""), "page": p.get("name", "")}
    raise UserError("Не нашёл Instagram-аккаунт. Проверьте, что аккаунт Instagram профессиональный и привязан к "
                    "странице Facebook, и что при создании токена вы отметили эту страницу и все разрешения из инструкции.")


def _connect_ig(app_secret: str, token: str) -> dict:
    """A token made on the «API setup with Instagram login» page (starts with IG): no Facebook page is needed."""
    if app_secret:
        try:  # a short token becomes a 60-day one; tokens from that page usually are long already
            r = net.request("Instagram", "GET", f"{IG_GRAPH}/access_token", tries=1, timeout=60, params={
                "grant_type": "ig_exchange_token", "client_secret": app_secret, "access_token": token}).json()
            token = r.get("access_token") or token
        except UserError:
            pass
    try:
        me = net.request("Instagram", "GET", f"{IG_GRAPH}/me", tries=2, timeout=60,
                         params={"fields": "user_id,username", "access_token": token}).json()
    except UserError as e:
        raise UserError("Instagram не принял маркер. Сгенерируйте его заново на странице «Настройка API для входа в "
                        f"Instagram» (кнопка «Сгенерировать маркер» рядом с аккаунтом) и вставьте целиком. Ответ: {e}")
    uid = str(me.get("user_id") or me.get("id") or "")
    if not uid:
        raise UserError(f"Instagram не прислал номер аккаунта: {str(me)[:200]}")
    save_settings({"ig_token": token, "ig_user_id": uid, "ig_username": me.get("username", ""), "ig_page": "",
                   "ig_login": "instagram", "ig_token_time": time.time()})
    return {"username": me.get("username", ""), "page": ""}


def _refresh_ig(s: dict) -> dict:
    """Instagram-login tokens live 60 days; each refresh gives 60 more, so regular use never lets them expire."""
    if s.get("ig_login") != "instagram" or time.time() - float(s.get("ig_token_time") or 0) < 7 * 86400:
        return s
    try:
        r = net.request("Instagram", "GET", f"{IG_GRAPH}/refresh_access_token", tries=2, timeout=60,
                        params={"grant_type": "ig_refresh_token", "access_token": s["ig_token"]}).json()
        if r.get("access_token"):
            save_settings({"ig_token": r["access_token"], "ig_token_time": time.time()})
            return load_settings()
    except UserError:
        pass
    return s


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


def _create_container(url: str, params: dict) -> str:
    """Creates the Reel container for a file sent from this computer. Instagram reads the fields from the address
    line (in the form body it may miss upload_type and ask for a video link instead); as a last try only the
    required fields are sent."""
    tries = [{"params": params}, {"data": params},
             {"params": {k: v for k, v in params.items() if k not in ("is_ai_generated", "share_to_feed")}}]
    last = None
    for kw in tries:
        try:
            r = net.request("Instagram", "POST", url, tries=2, timeout=120, **kw)
        except UserError as e:
            if "video_url" not in str(e):
                raise
            last = e
            continue
        cid = r.json().get("id")
        if not cid:
            raise UserError(f"Instagram не создал рилс: {r.text[:300]}")
        return cid
    raise UserError("Instagram не принимает загрузку видео прямо с компьютера для этого подключения. "
                    f"Напишите об этом, приложив текст ошибки: {last}")


def publish(video: Path, text: str, say=None) -> dict:
    """Publishes a Reel. Returns {"media_id", "url"}."""
    s = _refresh_ig(load_settings())
    token, uid = _auth(s)
    say = say or (lambda msg: None)
    ig_login = s.get("ig_login") == "instagram"
    if ig_login:
        # Meta allows uploading a file from the computer only for apps connected through Facebook Login;
        # the Instagram-login API takes a public video link only, and the video is never put on a public host.
        raise UserError("Это подключение Instagram (маркер со страницы «Настройка API для входа в Instagram») не умеет "
                        "загружать видео с компьютера: так устроено у Meta. Подключите Instagram заново способом "
                        "через страницу Facebook (Настройки → Instagram → «Подключить через Facebook»).")
    base = f"{IG_GRAPH}/{IG_VERSION}" if ig_login else GRAPH
    rupload = f"https://rupload.facebook.com/ig-api-upload/{IG_VERSION}" if ig_login else RUPLOAD
    params = {"media_type": "REELS", "upload_type": "resumable", "caption": text, "share_to_feed": "true",
              "access_token": token}
    if s.get("ig_ai_label", True):
        params["is_ai_generated"] = "true"
    cid = _create_container(f"{base}/{uid}/media", params)
    if not cid:
        raise UserError(f"Instagram не создал рилс: {r.text[:300]}")
    say("загружаю видео")
    data = video.read_bytes()
    up = net.request("Instagram", "POST", f"{rupload}/{cid}", content=data, tries=3, timeout=900, headers={
        "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(len(data))})
    if not (up.json() or {}).get("success"):
        raise UserError(f"Instagram не принял файл: {up.text[:300]}")
    say("Instagram обрабатывает видео")
    deadline = time.time() + 15 * 60
    while True:
        time.sleep(15)
        st = net.request("Instagram", "GET", f"{base}/{cid}", tries=3, timeout=60,
                         params={"fields": "status_code,status", "access_token": token}).json()
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise UserError(f"Instagram не смог обработать видео: {st.get('status') or code}")
        if time.time() > deadline:
            raise UserError("Instagram слишком долго обрабатывает видео. Попробуйте ещё раз позже.")
    say("публикую")
    r = net.request("Instagram", "POST", f"{base}/{uid}/media_publish", tries=2, timeout=120,
                    params={"creation_id": cid, "access_token": token})
    mid = r.json().get("id")
    if not mid:
        raise UserError(f"Instagram не опубликовал рилс: {r.text[:300]}")
    url = ""
    try:
        url = net.request("Instagram", "GET", f"{base}/{mid}", tries=2, timeout=60,
                          params={"fields": "permalink", "access_token": token}).json().get("permalink", "")
    except UserError:
        pass
    return {"media_id": mid, "url": url or f"https://www.instagram.com/{s.get('ig_username', '')}"}
