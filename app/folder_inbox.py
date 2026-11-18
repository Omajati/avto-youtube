"""Автоподхват проектов из «Папки входящих» (обычно папка Google Drive на Mac).

Устройство папок:
    inbox/<канал>/<проект>.zip        — архив сценария (плоский формат «Сборки из папок»)
    inbox/<канал>/<проект>/           — или уже распакованная папка
    work/<канал>/<проект>/            — сюда программа распаковывает или копирует проект

Готовый архив (или папка) переезжает в inbox/<канал>/_обработано/, неудачный — в inbox/<канал>/_ошибки/
вместе с файлом «… .ошибка.txt», где написана причина.

Модуль не зависит от fastapi и остальной программы: всё, что нужно снаружи (проверка, что папка — проект,
и постановка в очередь), передаётся функциями в ``scan_once``. Так один проход можно проверить отдельно.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import unicodedata
import zipfile
from pathlib import Path
from typing import Callable

DONE_DIR = "_обработано"
ERROR_DIR = "_ошибки"
STABLE_SECONDS = 30
# Временные и служебные файлы синхронизации Google Drive, браузеров и macOS.
TEMP_SUFFIXES = (".tmp", ".temp", ".partial", ".part", ".gdownload", ".crdownload", ".download", ".gdoc~")
SKIP_NAMES = {".ds_store", "thumbs.db", "desktop.ini", "icon\r", "__macosx"}

_scan_lock = threading.Lock()


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def is_temp_name(name: str) -> bool:
    """Скрытые, временные и служебные имена, которые нельзя брать в работу."""
    low = _nfc(name).lower()
    if not low or low.startswith((".", "~", "._")) or low in SKIP_NAMES:
        return True
    return low.endswith(TEMP_SUFFIXES) or ".gdownload" in low or low.startswith("~$")


def _read_json(path: Path, default):
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def _write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(path)


def load_state(state_file: Path) -> dict:
    state = _read_json(Path(state_file), {})
    state.setdefault("done", {})       # ключ архива/папки → итог; повторно не берём
    state.setdefault("pending", {})    # ключ → {"sig": [...], "since": время}; ждём, пока перестанет меняться
    state.setdefault("picked", 0)
    state.setdefault("errors", 0)
    state.setdefault("last_check", 0)
    state.setdefault("last_error", "")
    state.setdefault("last_picked", "")
    state.setdefault("status", "")
    return state


def _signature(path: Path) -> list | None:
    """Размер и время изменения; для папки — по всем файлам. None — внутри идёт синхронизация."""
    st = path.stat()
    if path.is_file():
        return [st.st_size, int(st.st_mtime)]
    size, mtime, count = 0, int(st.st_mtime), 0
    for base, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            low = name.lower()
            if low.endswith(TEMP_SUFFIXES) or ".gdownload" in low or low.startswith("~$"):
                return None
            try:
                fst = (Path(base) / name).stat()
            except OSError:
                return None
            size += fst.st_size
            mtime = max(mtime, int(fst.st_mtime))
            count += 1
    return [size, mtime, count]


def _unique(target: Path) -> Path:
    if not target.exists():
        return target
    stem, suffix = (target.stem, target.suffix) if target.is_file() or target.suffix.lower() == ".zip" else (target.name, "")
    n = 2
    while True:
        candidate = target.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def _move_aside(item: Path, folder: str, reason: str = "") -> Path | None:
    dest_dir = item.parent / folder
    try:
        dest_dir.mkdir(exist_ok=True)
        dest = _unique(dest_dir / item.name)
        shutil.move(str(item), str(dest))
        if reason:
            note = dest_dir / f"{dest.name}.ошибка.txt"
            note.write_text(
                f"Проект «{item.name}» не взят в работу.\n\nПричина: {reason}\n\n"
                f"Что делать: исправьте и положите архив (или папку) снова в папку канала «{item.parent.name}».\n"
                f"Время: {time.strftime('%d.%m.%Y %H:%M:%S')}\n", "utf-8")
        return dest
    except OSError:
        return None


class InboxError(Exception):
    """Понятная владельцу причина, почему архив не взят."""


def _member_name(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if not info.flag_bits & 0x800:
        # Архивы с Mac часто пишут UTF-8 без флага: zipfile тогда прочитал имя как cp437.
        try:
            name = name.encode("cp437").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return _nfc(name.replace("\\", "/"))


def _zip_plan(archive: Path) -> tuple[str, list[tuple[zipfile.ZipInfo, list[str]]]]:
    """Имя проекта и список (запись, части пути внутри проекта) без мусора macOS."""
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as exc:
        raise InboxError(f"файл повреждён или это не ZIP-архив ({exc}). Возможно, он ещё не докачался — запакуйте заново.") from exc
    with zf:
        bad = zf.testzip()
        if bad:
            raise InboxError(f"архив повреждён: не читается файл «{_member_name(zf.getinfo(bad))}». Запакуйте заново.")
        entries = []
        for info in zf.infolist():
            name = _member_name(info)
            if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
                raise InboxError(f"в архиве путь от корня диска «{name}» — такой архив небезопасно распаковывать.")
            parts = [p for p in name.split("/") if p not in ("", ".")]
            if any(p == ".." for p in parts):
                raise InboxError(f"в архиве путь с «..» («{name}») — такой архив небезопасно распаковывать.")
            if not parts or any(p.lower() == "__macosx" or p.startswith("._") or p.lower() == ".ds_store" for p in parts):
                continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:  # ссылка — пропускаем
                continue
            entries.append((info, parts))
    files = [(i, p) for i, p in entries if not i.is_dir()]
    if not files:
        raise InboxError("архив пустой.")
    roots = {p[0] for _i, p in entries}
    if len(roots) == 1 and all(len(p) >= 2 for _i, p in files):
        project = next(iter(roots))
        entries = [(i, p[1:]) for i, p in entries if len(p) > 1]
    else:
        project = _nfc(archive.stem)
    return project, entries


def _extract(archive: Path, entries, target: Path):
    tmp = target.parent / f".распаковка-{target.name}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    root = tmp.resolve()
    try:
        with zipfile.ZipFile(archive) as zf:
            for info, parts in entries:
                out = tmp.joinpath(*parts)
                if root not in out.resolve().parents and out.resolve() != root:
                    raise InboxError(f"путь «{'/'.join(parts)}» выходит за папку проекта.")
                if info.is_dir():
                    out.mkdir(parents=True, exist_ok=True)
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
        tmp.rename(target)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _copy_folder(source: Path, target: Path):
    tmp = target.parent / f".копирование-{target.name}"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        shutil.copytree(source, tmp, symlinks=False,
                        ignore=lambda _d, names: [n for n in names if is_temp_name(n) or n.lower() == "__macosx"])
        tmp.rename(target)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _project_name_ok(name: str) -> bool:
    return bool(name) and name not in (".", "..") and "/" not in name and not name.startswith(".")


def scan_once(inbox: Path, work: Path, state: dict, *,
              is_project: Callable[[Path], bool],
              enqueue: Callable[[Path, str], None],
              now: float | None = None,
              stable_seconds: float = STABLE_SECONDS,
              work_for: Callable[[str], Path] | None = None,
              log: Callable[[str], None] = lambda _m: None) -> dict:
    """Один проход наблюдателя. Меняет и возвращает ``state``; сохраняет его вызывающий.

    ``is_project(path)`` — есть ли в папке сцены и озвучка;
    ``work_for(channel)`` — рабочая папка канала (своя у канала или общая ``work``);
    ``enqueue(path, channel)`` — поставить готовую папку в очередь (ошибка → проект в «_ошибки» не уходит,
    но счётчик ошибок растёт).
    """
    with _scan_lock:
        now = time.time() if now is None else now
        inbox, work = Path(inbox).expanduser(), Path(work).expanduser()
        state["last_check"] = int(now)
        if not inbox.is_dir():
            state["status"] = f"Папка входящих не найдена: {inbox}"
            return state
        try:
            inbox_r, work_r = inbox.resolve(), work.resolve()
        except OSError as exc:
            state["status"] = f"Не открывается папка: {exc}"
            return state
        if work_r == inbox_r or inbox_r in work_r.parents or work_r in inbox_r.parents:
            state["status"] = "Рабочая папка не должна быть внутри папки входящих (и наоборот). Выберите другую."
            return state
        try:
            work.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            state["status"] = f"Не получилось создать рабочую папку: {exc}"
            return state
        state["status"] = ""
        seen_pending = set()
        for channel_dir in sorted(inbox.iterdir()):
            if not channel_dir.is_dir() or is_temp_name(channel_dir.name) or channel_dir.name.startswith("_"):
                continue
            channel = _nfc(channel_dir.name)
            try:
                items = sorted(channel_dir.iterdir())
            except OSError:
                continue
            for item in items:
                name = item.name
                if is_temp_name(name) or name.startswith("_"):
                    continue
                is_zip = item.is_file() and name.lower().endswith(".zip")
                if not is_zip and not item.is_dir():
                    continue
                try:
                    sig = _signature(item)
                except OSError:
                    continue
                key = f"{channel}/{_nfc(name)}"
                if sig is None:
                    state["pending"].pop(key, None)
                    continue
                done_key = f"{key}|{sig[0]}|{sig[1]}"
                if done_key in state["done"]:
                    continue
                seen_pending.add(key)
                wait = state["pending"].get(key)
                if not wait or wait.get("sig") != sig:
                    state["pending"][key] = {"sig": sig, "since": now}
                    continue
                if now - wait.get("since", now) < stable_seconds:
                    continue
                if not is_zip and not is_project(item):
                    continue  # обычная папка без сцен и озвучки — не трогаем
                state["pending"].pop(key, None)
                seen_pending.discard(key)
                base = work
                if work_for:
                    try:
                        base = Path(work_for(channel)).expanduser()
                        base.mkdir(parents=True, exist_ok=True)
                    except Exception:
                        base = work
                _take(item, is_zip, channel, base, state, done_key, is_project, enqueue, log)
        for key in list(state["pending"]):
            if key not in seen_pending:
                state["pending"].pop(key, None)
        return state


def _take(item: Path, is_zip: bool, channel: str, work: Path, state: dict, done_key: str,
          is_project, enqueue, log):
    kind = "архив" if is_zip else "папка"
    target = None
    try:
        if is_zip:
            project, entries = _zip_plan(item)
        else:
            project, entries = _nfc(item.name), None
        if not _project_name_ok(project):
            raise InboxError(f"странное имя проекта «{project}».")
        target = work / channel / project
        if target.exists():
            target = None
            raise InboxError(f"проект «{project}» уже есть в рабочей папке ({work / channel / project}). "
                             "Если его надо собрать заново — удалите или переименуйте старую папку.")
        target.parent.mkdir(parents=True, exist_ok=True)
        if is_zip:
            _extract(item, entries, target)
        else:
            _copy_folder(item, target)
        if not is_project(target):
            raise InboxError("в проекте не нашлось файла сцен (сцены_генерация.txt) или текста озвучки (озвучка_XX.txt).")
    except Exception as exc:
        if target is not None and target.exists():
            shutil.rmtree(target, ignore_errors=True)
        reason = str(exc) if isinstance(exc, InboxError) else f"не получилось распаковать: {exc}"
        moved = _move_aside(item, ERROR_DIR, reason)
        state["done"][done_key] = {"at": int(time.time()), "result": "error", "reason": reason,
                                   "moved": str(moved or "")}
        state["errors"] = int(state.get("errors") or 0) + 1
        state["last_error"] = f"{channel} / {item.name}: {reason}"
        log(f"Автоподхват: {kind} {item} не взят: {reason}")
        return
    moved = _move_aside(item, DONE_DIR)
    state["done"][done_key] = {"at": int(time.time()), "result": "ok", "path": str(target), "moved": str(moved or "")}
    state["picked"] = int(state.get("picked") or 0) + 1
    state["last_picked"] = f"{channel} / {target.name}"
    try:
        enqueue(target, channel)
    except Exception as exc:
        state["errors"] = int(state.get("errors") or 0) + 1
        state["last_error"] = f"{channel} / {target.name}: распакован, но не поставлен в очередь: {exc}"
        log(state["last_error"])


# ---------------------------------------------------------------- сценарии из репозитория GitHub

def normalize_repo(raw: str) -> str:
    """«owner/name», ссылка https://github.com/owner/name(.git) или git@github.com:owner/name → owner/name."""
    text = (raw or "").strip().rstrip("/")
    for prefix in ("https://github.com/", "http://github.com/", "github.com/", "git@github.com:"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
    if text.endswith(".git"):
        text = text[:-4]
    parts = [p for p in text.split("/") if p]
    return "/".join(parts[:2]) if len(parts) >= 2 else ""


def github_pull(tree: list[dict], inbox: Path, state: dict, download: Callable[[str], bytes],
                log: Callable[[str], None] = lambda _m: None) -> int:
    """Скачивает новые «<канал>/<проект>.zip» из дерева репозитория в inbox/<канал>/.

    ``tree`` — записи из GitHub API (path, type, sha); ``download(path)`` возвращает содержимое файла.
    Уже скачанные версии (по sha) повторно не берутся. Возвращает число скачанных архивов.
    """
    seen = state.setdefault("github", {})
    got = 0
    for entry in tree:
        path = str(entry.get("path") or "")
        parts = path.split("/")
        if entry.get("type") != "blob" or len(parts) != 2 or not parts[1].lower().endswith(".zip"):
            continue
        channel, name = _nfc(parts[0]), _nfc(parts[1])
        if is_temp_name(channel) or channel.startswith("_") or is_temp_name(name) or not _project_name_ok(channel):
            continue
        sha = str(entry.get("sha") or "")
        if not sha or seen.get(path) == sha:
            continue
        target_dir = Path(inbox) / channel
        target_dir.mkdir(parents=True, exist_ok=True)
        tmp = target_dir / f".{name}.download"
        try:
            data = download(path)
            tmp.write_bytes(data)
            os.replace(tmp, target_dir / name)
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            state["github_status"] = f"Не скачался «{path}»: {exc}"
            log(state["github_status"])
            continue  # попробуем в следующий раз
        seen[path] = sha
        got += 1
        state["github_downloaded"] = int(state.get("github_downloaded") or 0) + 1
        log(f"GitHub: скачан {path}")
    return got
