"""Several ElevenLabs keys (accounts): when the credits of the working one run out, the next one is taken by itself.
The reserve keys live in their own file, never in the settings the page reads. Library voices are personal to an
account, so a voice added from the library is remembered and added again to the new account when it is needed."""
import hashlib
import json
import threading
import time

from .config import DATA_DIR, load_settings, save_settings

FILE = DATA_DIR / "eleven_keys.json"
_lock = threading.RLock()


def fp(key: str) -> str:
    return hashlib.sha256((key or "").encode()).hexdigest()[:12]


def mask(key: str) -> str:
    return f"{key[:5]}…{key[-4:]}" if len(key or "") > 12 else "•••"


def _read() -> dict:
    try:
        return json.loads(FILE.read_text("utf-8"))
    except Exception:
        return {}


def _write(d: dict):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(FILE)


def reserve() -> list:
    return list(_read().get("reserve") or [])


def all_keys() -> list:
    """The working key first, then the reserve, without repeats."""
    out = []
    for k in [load_settings().get("elevenlabs_key", "")] + reserve():
        if k and k not in out:
            out.append(k)
    return out


def add(key: str):
    key = (key or "").strip()
    with _lock:
        d = _read()
        keys = d.setdefault("reserve", [])
        if key and key not in keys and key != load_settings().get("elevenlabs_key"):
            keys.append(key)
        _write(d)


def remove(key: str):
    with _lock:
        d = _read()
        d["reserve"] = [k for k in d.get("reserve") or [] if k != key]
        _write(d)


def use(key: str):
    """Makes this key the working one; the old working key goes to the end of the reserve."""
    with _lock:
        d = _read()
        old = load_settings().get("elevenlabs_key", "")
        keys = [k for k in d.get("reserve") or [] if k != key]
        if old and old != key:
            keys.append(old)
        d["reserve"] = keys
        (d.setdefault("spent", {})).pop(fp(key), None)
        _write(d)
        save_settings({"elevenlabs_key": key})


def spent_at(key: str) -> float:
    return float((_read().get("spent") or {}).get(fp(key), 0))


def switch(settings: dict, failed: str) -> bool:
    """The credits of `failed` ran out: the next key with credits becomes the working one (in `settings` too).
    Another video may have switched already: then its key is simply taken. Returns False when no key is left."""
    with _lock:
        cur = load_settings().get("elevenlabs_key", "")
        if cur and cur != failed and time.time() - spent_at(cur) > 6 * 3600:
            settings["elevenlabs_key"] = cur
            return True
        d = _read()
        d.setdefault("spent", {})[fp(failed)] = time.time()
        _write(d)
        # A key marked empty more than 6 hours ago is tried again: its month may have renewed.
        nxt = next((k for k in reserve() if k != failed and time.time() - spent_at(k) > 6 * 3600), None)
        if not nxt:
            return False
        use(nxt)
        d = _read()
        d.setdefault("spent", {})[fp(failed)] = time.time()
        _write(d)
        settings["elevenlabs_key"] = nxt
        return True


def remember_voice(voice_id: str, owner: str, public_id: str, name: str, key: str):
    """A library voice added to an account: enough to add it again to another account."""
    with _lock:
        d = _read()
        d.setdefault("voices", {})[voice_id] = {"owner": owner, "public_id": public_id, "name": name}
        d.setdefault("map", {}).setdefault(fp(key), {})[voice_id] = voice_id
        _write(d)


def voice_for(key: str, voice_id: str) -> str:
    """The id of this voice in the account of `key` (library voices get their own id in each account)."""
    return ((_read().get("map") or {}).get(fp(key)) or {}).get(voice_id, voice_id)


def known_voice(voice_id: str) -> dict:
    return (_read().get("voices") or {}).get(voice_id) or {}


def set_voice(key: str, voice_id: str, new_id: str):
    with _lock:
        d = _read()
        d.setdefault("map", {}).setdefault(fp(key), {})[voice_id] = new_id
        _write(d)
