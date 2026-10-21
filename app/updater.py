"""Updates from GitHub: the program asks the repository for the newest version and installs it with one button.
release/latest.json in the repository says which version is newest; the code itself comes from the main branch."""
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from . import net
from .config import APP_DIR
from .net import UserError

REPO = "Omajati/avto-youtube"
LATEST = f"https://raw.githubusercontent.com/{REPO}/main/release/latest.json"
CODE_ZIP = f"https://codeload.github.com/{REPO}/zip/refs/heads/main"
BUNDLE_ID = "ru.autoyoutube.app"
# What an update replaces in ~/AutoYouTube (the data folder is never touched).
PARTS = ["app", "prompts_default", "run.py", "requirements.txt", "README.md", "О программе Авто ютуб.txt"]

state = {"status": "idle", "step": "", "error": ""}
_lock = threading.Lock()


def current() -> int:
    f = APP_DIR / "VERSION"
    if f.exists():
        try:
            return int(f.read_text().strip())
        except ValueError:
            pass
    m = re.search(r"\((\d+)\)", (APP_DIR / "app" / "static" / "index.html").read_text("utf-8"))
    return int(m.group(1)) if m else 0


def can_install() -> bool:
    """Only the Mac app installs updates itself; elsewhere the files are replaced by hand."""
    return sys.platform == "darwin" and (APP_DIR / ".venv").exists()


def check() -> dict:
    r = net.request("GitHub", "GET", LATEST, tries=2, timeout=20, params={"t": int(time.time() // 300)}).json()
    latest = int(r.get("version") or 0)
    return {"current": current(), "latest": latest, "available": latest > current(), "label": r.get("label", ""),
            "notes": r.get("notes", ""), "can_install": can_install(), "download": r.get("app_zip", "")}


def start():
    with _lock:
        if state["status"] == "working":
            raise UserError("Обновление уже идёт.")
        if not can_install():
            raise UserError("Обновить сама может только программа, установленная как приложение на Mac.")
        state.update(status="working", step="готовлю", error="")
    threading.Thread(target=_run, daemon=True).start()


def _say(step):
    state["step"] = step


def _run():
    try:
        info = check()
        if not info["available"]:
            raise UserError("У вас уже последняя версия.")
        _say("сохраняю резервную копию настроек")
        try:
            from . import backup
            backup.make("перед обновлением")
        except Exception:
            pass
        _say("скачиваю новую версию")
        data = net.request("GitHub", "GET", CODE_ZIP, tries=3, timeout=180).content
        tmp = Path(tempfile.mkdtemp(prefix="ay-update-"))
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            z.extractall(tmp)
        src = next((d for d in tmp.iterdir() if (d / "app" / "server.py").exists()), None)
        if not src:
            raise UserError("В скачанной версии нет файлов программы. Попробуйте позже.")
        _say("ставлю новую версию")
        for part in PARTS:
            s, d = src / part, APP_DIR / part
            if not s.exists():
                continue
            if s.is_dir():
                old = APP_DIR / f".{part}.old"
                shutil.rmtree(old, ignore_errors=True)
                if d.exists():
                    d.rename(old)
                shutil.copytree(s, d)
                shutil.rmtree(old, ignore_errors=True)
            else:
                shutil.copy2(s, d)
        _say("обновляю библиотеки")
        uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
        r = subprocess.run([uv, "pip", "install", "--python", str(APP_DIR / ".venv" / "bin" / "python"), "-q", "-r",
                            str(APP_DIR / "requirements.txt")], capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            raise UserError(f"Не удалось обновить библиотеки: {(r.stderr or r.stdout)[-400:]}")
        _update_bundle(src, info["latest"])
        (APP_DIR / "VERSION").write_text(str(info["latest"]))
        shutil.rmtree(tmp, ignore_errors=True)
        _say("перезапускаю программу")
        state["status"] = "restarting"
        _restart()
    except UserError as e:
        state.update(status="error", error=str(e))
    except Exception as e:
        state.update(status="error", error=f"Непредвиденная ошибка: {e}")


def _bundle() -> Path:
    env = os.environ.get("AY_APP")
    cands = [Path(env)] if env else []
    cands += [Path("/Applications/Авто ютуб.app"), Path.home() / "Applications" / "Авто ютуб.app"]
    try:
        out = subprocess.run(["mdfind", f"kMDItemCFBundleIdentifier == '{BUNDLE_ID}'"], capture_output=True,
                             text=True, timeout=10).stdout.split("\n")
        cands += [Path(x) for x in out if x.endswith(".app") and "AppTranslocation" not in x]
    except Exception:
        pass
    return next((c for c in cands if (c / "Contents" / "MacOS" / "launch").exists() and os.access(c / "Contents", os.W_OK)), None)


def _update_bundle(src: Path, version: int):
    """The app keeps a copy of the program for the first start; it is brought up to date too, so starting the app
    never puts an older version back."""
    b = _bundle()
    if not b:
        return
    payload = b / "Contents" / "Resources" / "payload"
    new = b / "Contents" / "Resources" / "payload.new"
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir(parents=True)
    for part in PARTS:
        s = src / part
        if s.is_dir():
            shutil.copytree(s, new / part)
        elif s.exists():
            shutil.copy2(s, new / part)
    shutil.copy2(src / "mac" / "setup.sh", new / "setup.sh")
    (new / "VERSION").write_text(str(version))
    old = b / "Contents" / "Resources" / "payload.old"
    shutil.rmtree(old, ignore_errors=True)
    if payload.exists():
        payload.rename(old)
    new.rename(payload)
    shutil.rmtree(old, ignore_errors=True)
    launch = b / "Contents" / "MacOS" / "launch"
    shutil.copy2(src / "mac" / "launch", launch)
    launch.chmod(0o755)
    plist = b / "Contents" / "Info.plist"
    try:
        t = plist.read_text("utf-8")
        t = re.sub(r"(<key>CFBundle(?:Short)?Version(?:String)?</key><string>)[^<]*", rf"\g<1>{version}", t)
        plist.write_text(t, "utf-8")
    except OSError:
        pass


def _restart():
    """Starts the app again a moment after this copy has quit."""
    b = _bundle()
    target = ["open", str(b)] if b else ["open", "-b", BUNDLE_ID]
    subprocess.Popen(["/bin/bash", "-c", f"sleep 3; {' '.join(_q(x) for x in target)}"], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    threading.Timer(1.0, lambda: os._exit(0)).start()


def _q(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"
