"""RoyalTechno (api.royaltechno.cc): a reseller of video, picture and voice models with one key and one balance.
Every request is a job: submitted once (with an idempotency key, so a retry is never billed twice), then polled until
its output file is ready. The key is created in their Telegram bot @royaltechno_ai_bot with /api_key_new."""
import base64
import io
import time
import uuid
from pathlib import Path

from . import net
from .net import UserError

BASE = "https://api.royaltechno.cc/v1"
BOT = "https://t.me/royaltechno_ai_bot"


def key(settings) -> str:
    k = settings.get("royal_key")
    if not k:
        raise UserError("Нужен ключ RoyalTechno: создайте его в их Телеграм-боте @royaltechno_ai_bot командой "
                        "/api_key_new и вставьте в Настройках (поле «Ключ RoyalTechno»).")
    return k


def data_uri(img, limit: int = 0) -> str:
    """A picture (path or PIL image) as a JPEG data URI."""
    from PIL import Image
    im = img if isinstance(img, Image.Image) else Image.open(img)
    im = im.convert("RGB")
    if limit:
        im.thumbnail((limit, limit))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _submit(k: str, model: str, inp: dict) -> dict:
    return net.request("RoyalTechno", "POST", f"{BASE}/jobs", json={"model": model, "input": inp}, timeout=120, tries=3,
                       headers={"Authorization": f"Bearer {k}", "Idempotency-Key": str(uuid.uuid4())}).json()


def run(settings, model: str, inp: dict, check=None, poll: float = 5, limit: float = 8 * 60,
        optional: tuple = ()) -> dict:
    """Submits one job and waits for it. Returns its output ({"url", "mime_type", ...}). optional: input fields the
    model may refuse (a length, a shape); on "invalid input" the job is sent once more without them."""
    k = key(settings)
    try:
        job = _submit(k, model, inp)
    except UserError as e:
        if "invalid" not in str(e).lower() or not any(f in inp for f in optional):
            raise
        job = _submit(k, model, {f: v for f, v in inp.items() if f not in optional})
    jid = job.get("id")
    if not jid:
        raise UserError(f"RoyalTechno не принял задание: {str(job.get('error') or job)[:300]}")
    headers = {"Authorization": f"Bearer {k}"}
    started = time.time()
    while True:
        st = job if job.get("status") in ("succeeded", "failed", "canceled") else \
            net.request("RoyalTechno", "GET", f"{BASE}/jobs/{jid}", headers=headers, timeout=30, tries=3).json()
        job = {}
        status = st.get("status")
        if status == "succeeded":
            out = st.get("output") or {}
            if not out.get("url"):
                raise UserError(f"RoyalTechno не вернул файл: {str(st)[:300]}")
            return out
        if status in ("failed", "canceled"):
            err = st.get("error") or {}
            msg = err.get("message") or err.get("code") or status if isinstance(err, dict) else err
            raise UserError(f"RoyalTechno: не получилось ({msg}). Деньги за неудачную попытку они возвращают сами.")
        if time.time() - started > limit:
            try:
                net.request("RoyalTechno", "POST", f"{BASE}/jobs/{jid}/cancel", headers=headers, timeout=20, tries=1)
            except UserError:
                pass
            raise UserError(f"RoyalTechno не успел за {int(limit // 60)} мин, задание отменено")
        if check:
            check()
        time.sleep(poll)


def fetch(url: str, minimum: int = 1000) -> bytes:
    data = net.request("RoyalTechno", "GET", url, timeout=300).content
    if len(data) < minimum:
        raise UserError("RoyalTechno вернул пустой файл. Попробуйте ещё раз.")
    return data


def balance(k: str) -> float:
    """Dollars left on the account (the reply's field names are not documented: the likely ones are tried)."""
    r = net.request("RoyalTechno", "GET", f"{BASE}/account", tries=1, timeout=15,
                    headers={"Authorization": f"Bearer {k}"}).json()
    for src in (r, r.get("balance") if isinstance(r.get("balance"), dict) else {}, r.get("wallet") or {}):
        for f, div in (("balance_usd_cents", 100), ("usd_cents", 100), ("balance_cents", 100), ("cents", 100),
                       ("balance_usd", 1), ("usd", 1), ("amount", 1)):
            if isinstance(src.get(f), (int, float)):
                return src[f] / div
    if isinstance(r.get("balance"), (int, float)):
        return float(r["balance"])
    raise UserError("RoyalTechno не прислал баланс.")


def save(data: bytes, path: Path):
    tmp = path.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(path)
