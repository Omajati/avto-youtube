"""Finished videos also appear in an ordinary folder the owner can find in Finder:
~/Movies/Авто ютуб/<project>/<date> <title>/. Files are hard links to the program's own copies, so they take
no extra disk space; they are refreshed whenever a video is assembled again or sped up."""
import os
import re
import shutil
import threading
import time
from pathlib import Path

from . import db, projects

ROOT = Path.home() / "Movies" / "Авто ютуб"
JOB_FILES = ("video.mp4", "short.mp4", "thumbnail.jpg", "youtube.txt", "subtitles.srt", "short_subtitles.srt",
             "description.txt")
_lock = threading.Lock()


def _safe(name: str, limit: int = 80) -> str:
    name = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", name or "").strip(" .")
    return re.sub(r"\s+", " ", name)[:limit].strip() or "Без названия"


def _place(src: Path, dst: Path, copy: bool):
    try:
        if dst.exists() and os.path.samefile(src, dst):
            return
    except OSError:
        pass
    dst.unlink(missing_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        if copy:
            shutil.copy2(src, dst)


def _sync(folder: Path, files: dict, copy: bool) -> Path:
    """files: {name in the folder: source path}. Old files of this video that no longer exist are removed."""
    present = {n: p for n, p in files.items() if p.exists()}
    if not present:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    for n, p in present.items():
        _place(p, folder / n, copy)
    for n in files:
        if n not in present:
            (folder / n).unlink(missing_ok=True)
    return folder


def _folder(project: str, created: float, title: str, key: str) -> Path:
    """The video's own folder; a video keeps its folder (marked with a .id file) when it is exported again."""
    base = ROOT / _safe(project, 60)
    if base.exists():
        for f in base.iterdir():
            try:
                if (f / ".id").read_text("utf-8").strip() == key:
                    return f
            except OSError:
                continue
    name = f"{time.strftime('%Y-%m-%d', time.localtime(created or time.time()))} {_safe(title)}"
    f = base / name
    return f if not f.exists() else base / f"{name} ({key[:4]})"


def export_job(jid: str, copy: bool = True) -> Path:
    from .pipeline import job_dir
    with _lock:
        j = db.get(jid)
        if not j:
            return None
        o = j.get("options") or {}
        proj = projects.get(o.get("project_id", "")) if o.get("project_id") else {}
        title = o.get("title_ru") or j.get("title") or o.get("topic") or j.get("topic") or jid
        if o.get("translate_from"):
            title += " (EN)"
        out = job_dir(jid) / "output"
        folder = _folder(proj.get("name") or "Без проекта", j.get("created"), title, jid)
        done = _sync(folder, {n: out / n for n in JOB_FILES}, copy)
        if done:
            (done / ".id").write_text(jid, "utf-8")
        return done


def export_remake(rid: str, meta: dict, out: Path, copy: bool = True) -> Path:
    with _lock:
        title = meta.get("title_ru") or meta.get("title") or meta.get("name") or rid
        folder = _folder("Переделки", meta.get("created"), title, rid)
        done = _sync(folder, {n: out / n for n in ("video.mp4", "thumbnail.jpg", "youtube.txt", "subtitles.srt")}, copy)
        if done:
            (done / ".id").write_text(rid, "utf-8")
        return done


def backfill():
    """Once at start: earlier finished videos appear in the folder too (only as hard links, never copies)."""
    mark = ROOT / ".old_videos_added"
    if mark.exists():
        return

    def work():
        for j in db.list_all():
            try:
                if j.get("stage") == "done":
                    export_job(j["id"], copy=False)
            except Exception:  # noqa: BLE001 - a missing old file must not stop the program
                pass
        try:
            ROOT.mkdir(parents=True, exist_ok=True)
            mark.write_text("1", "utf-8")
        except OSError:
            pass
    threading.Thread(target=work, daemon=True).start()
