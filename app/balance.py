"""Balances of the paid services. ElevenLabs and fal.ai report their balance themselves; Anthropic, OpenAI and the
other picture services do not, so the program counts its own spending there and subtracts it from the amount the
owner last copied from the service's billing page."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import net
from .config import DATA_DIR, load_settings
from .net import UserError

SPEND_FILE = DATA_DIR / "spend.json"
_lock = threading.Lock()
LOW_USD = 3.0

SERVICES = {
    "anthropic": {"name": "Claude (Anthropic)", "key": "anthropic_key",
                  "billing": "https://console.anthropic.com/settings/billing"},
    "openai": {"name": "OpenAI", "key": "openai_key",
               "billing": "https://platform.openai.com/settings/organization/billing/overview"},
    "elevenlabs": {"name": "ElevenLabs (озвучка)", "key": "elevenlabs_key",
                   "billing": "https://elevenlabs.io/app/subscription"},
    "fal": {"name": "fal.ai (картинки и анимация)", "key": "fal_key", "billing": "https://fal.ai/dashboard/billing"},
    "replicate": {"name": "Replicate (картинки)", "key": "replicate_key", "billing": "https://replicate.com/account/billing"},
    "together": {"name": "Together (картинки)", "key": "together_key", "billing": "https://api.together.ai/settings/billing"},
    "runware": {"name": "Runware (картинки)", "key": "runware_key", "billing": "https://my.runware.ai/wallet"},
}


def _read() -> dict:
    try:
        return json.loads(SPEND_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _write(d: dict):
    tmp = SPEND_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(SPEND_FILE)


def record(service: str, usd: float):
    """Adds money the program just spent at a service (approximate, from its own price list)."""
    if not usd:
        return
    with _lock:
        d = _read()
        spent = d.setdefault("spent", {})
        spent[service] = round(spent.get(service, 0) + usd, 4)
        d.setdefault("since", time.time())
        _write(d)


def set_known(service: str, amount: float):
    """The owner copied the current balance from the billing page; spending is counted from this moment."""
    if service not in SERVICES:
        raise UserError("Неизвестный сервис.")
    with _lock:
        d = _read()
        d.setdefault("known", {})[service] = {"amount": round(float(amount), 2), "time": time.time(),
                                              "spent_at": d.get("spent", {}).get(service, 0)}
        _write(d)


def _elevenlabs(key: str) -> dict:
    r = net.request("ElevenLabs", "GET", "https://api.elevenlabs.io/v1/user/subscription", tries=1, timeout=15,
                    headers={"xi-api-key": key})
    try:
        r = r.json()
    except ValueError:  # a block page instead of the answer: ElevenLabs is closed for Russia without a VPN
        return {"kind": "error", "error": "ElevenLabs не ответил: включите VPN"}
    used, limit = int(r.get("character_count") or 0), int(r.get("character_limit") or 0)
    left = max(limit - used, 0)
    out = {"kind": "real", "value": left, "unit": "символов", "limit": limit,
           "minutes": round(left / 1000), "low": limit > 0 and left < limit * 0.1}
    if r.get("next_character_count_reset_unix"):
        out["reset"] = r["next_character_count_reset_unix"]
    if r.get("tier"):
        out["plan"] = r["tier"]
    return out


def _fal(key: str) -> dict:
    r = net.request("fal.ai", "GET", "https://api.fal.ai/v1/account/billing", tries=1, timeout=15,
                    params={"expand": "credits"}, headers={"Authorization": f"Key {key}"}).json()
    cr = r.get("credits") or {}
    if "current_balance" not in cr:
        raise UserError("fal.ai не прислал баланс.")
    val = float(cr["current_balance"])
    return {"kind": "real", "value": round(val, 2), "unit": cr.get("currency", "USD"), "low": val < LOW_USD}


def _estimate(service: str, d: dict) -> dict:
    spent = d.get("spent", {}).get(service, 0)
    known = d.get("known", {}).get(service)
    out = {"kind": "estimate", "spent": round(spent, 2), "since": d.get("since")}
    if known:
        val = round(known["amount"] - (spent - known.get("spent_at", 0)), 2)
        out.update(value=val, unit="USD", known_time=known["time"], known_amount=known["amount"], low=val < LOW_USD)
    return out


def check_all() -> list:
    s = load_settings()
    d = _read()
    todo = [sid for sid, svc in SERVICES.items() if s.get(svc["key"]) and
            not (sid in ("replicate", "together", "runware") and s.get("image_provider") != sid)]
    # The services are asked at the same time, so one slow service does not hold up the page.
    with ThreadPoolExecutor(max_workers=len(todo) or 1) as ex:
        return list(ex.map(lambda sid: _one(sid, s.get(SERVICES[sid]["key"]), d), todo))


def _one(sid: str, key: str, d: dict) -> dict:
    svc = SERVICES[sid]
    row = {"id": sid, "name": svc["name"], "billing": svc["billing"]}
    try:
        if sid == "elevenlabs":
            row.update(_elevenlabs(key))
        elif sid == "fal":
            try:
                row.update(_fal(key))
            except UserError as e:
                # Keys made before fal added billing rights may not be allowed to read the balance.
                row.update(_estimate(sid, d), error=str(e), fal_rights="нет связи" not in str(e))
        else:
            row.update(_estimate(sid, d))
    except UserError as e:
        row.update(kind="error", error=str(e))
    return row
