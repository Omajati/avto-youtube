"""Uploading a finished video through YouTube Studio inside an Octo Browser profile (the channel's own browser,
with its own IP and sign-in), not through the YouTube API.

The profile is started through Octo's local API with a debug port; Playwright attaches to that browser (CDP),
opens studio.youtube.com in a new tab, fills the upload dialog like a person would and waits until the file has
fully gone up before the tab is closed. Studio can show its interface in any language, so buttons are found by
their element ids and names, never by their text.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from .net import UserError

STUDIO = "https://studio.youtube.com"
PRIVACY = {"public": "PUBLIC", "unlisted": "UNLISTED", "private": "PRIVATE"}


class _Stop(Exception):
    pass


# YouTube category id -> the words that name it in the upload file and in Studio (EN, RU, ES, DE).
CATEGORIES = {
    "27": ["education", "образование", "educación", "bildung"],
    "22": ["people & blogs", "people and blogs", "люди и блоги", "gente y blogs", "personen & blogs", "personen und blogs"],
    "24": ["entertainment", "развлечения", "entretenimiento", "unterhaltung"],
    "15": ["pets & animals", "pets and animals", "животные", "mascotas y animales", "tiere"],
    "28": ["science & technology", "science and technology", "наука и техника", "ciencia y tecnología", "wissenschaft & technik"],
    "1": ["film & animation", "film and animation", "фильмы и анимация", "cine y animación", "film & animation"],
    "19": ["travel & events", "travel and events", "путешествия и события", "viajes y eventos", "reisen & events"],
    "25": ["news & politics", "новости и политика", "noticias y política", "nachrichten & politik"],
    "26": ["howto & style", "хобби и стиль", "consejos y estilo", "praktische tipps & styling"],
    "10": ["music", "музыка", "música", "musik"],
    "17": ["sports", "спорт", "deportes", "sport"],
    "20": ["gaming", "видеоигры", "videojuegos", "gaming"],
    "23": ["comedy", "юмор", "comedia", "comedy"],
    "29": ["nonprofits & activism", "некоммерческие организации", "ong y activismo"],
    "2": ["autos & vehicles", "транспорт", "motor", "autos & fahrzeuge"],
}


def category_id(text: str) -> str:
    """'People & Blogs (Gente y blogs). Язык видео…' -> '22'; '' when the line names no category."""
    low = (text or "").lower()
    m = re.search(r"\b(?:id\s*)?(\d{1,2})\b", low)
    if m and m.group(1) in CATEGORIES and re.match(r"\s*(?:id\s*)?\d", low):
        return m.group(1)
    best = ("", 10 ** 6)
    for cid, names in CATEGORIES.items():
        for n in names:
            i = low.find(n)
            if i >= 0 and i < best[1]:
                best = (cid, i)
    return best[0]


def _set_file(page, locator, path: Path):
    """Puts a file into an <input type=file>. Octo's browser runs on this same Mac but Playwright reaches it through
    the debug port and refuses to send files over 50 MB «to a browser not co-located with the server». So the
    browser is given the file's path straight through the DevTools protocol (DOM.setFileInputFiles): it reads the
    file from disk itself, any size, and fires the same change event as a person picking the file."""
    handle = locator.element_handle(timeout=90_000)
    cdp = page.context.new_cdp_session(page)
    try:
        handle.evaluate("e => { window.__ayFileInput = e; }")
        obj = cdp.send("Runtime.evaluate", {"expression": "window.__ayFileInput"})
        object_id = (obj.get("result") or {}).get("objectId")
        if not object_id:
            raise RuntimeError("не нашёл поле выбора файла")
        cdp.send("DOM.setFileInputFiles", {"files": [str(path.resolve())], "objectId": object_id})
    except Exception:  # noqa: BLE001
        if path.stat().st_size < 45 * 1024 * 1024:
            locator.set_input_files(str(path))  # small files: Playwright's own way still works
        else:
            raise
    finally:
        try:
            page.evaluate("() => { delete window.__ayFileInput; }")
            cdp.detach()
        except Exception:  # noqa: BLE001
            pass


def _expand(page, state: dict):
    """Opens «Show more» once (tags, category and the AI label live there)."""
    if state.get("open"):
        return
    page.locator("ytcp-button#toggle-button, #toggle-button").first.click()
    time.sleep(1)
    state["open"] = True


def _set_category(page, cid: str, log):
    names = CATEGORIES.get(cid) or []
    try:
        trigger = page.locator("ytcp-form-select#category ytcp-dropdown-trigger, #category ytcp-dropdown-trigger, "
                               "#category-container ytcp-dropdown-trigger").first
        trigger.scroll_into_view_if_needed(timeout=15_000)
        trigger.click(timeout=15_000)
        time.sleep(1)
        items = page.locator("tp-yt-paper-listbox tp-yt-paper-item:visible, ytcp-text-menu tp-yt-paper-item:visible")
        for sel in (f'tp-yt-paper-item[test-id="{cid}"]:visible', f'tp-yt-paper-item[test-id$="_{cid}"]:visible',
                    f'tp-yt-paper-item[value="{cid}"]:visible'):
            hit = page.locator(sel)
            if hit.count():
                hit.first.click()
                return True
        for i in range(items.count()):
            item = items.nth(i)
            text = (item.inner_text(timeout=3000) or "").strip().lower()
            if text and any(text == n or text.startswith(n) for n in names):
                item.click()
                return True
        page.keyboard.press("Escape")
    except Exception as e:  # noqa: BLE001
        log(f"Категорию поставить не получилось ({str(e).splitlines()[0][:120]}) — оставляю как есть")
        return False
    log("Категорию в списке Studio не нашёл — оставляю как есть")
    return False


def _set_altered(page, yes: bool):
    """«Altered or synthetic content» (the AI voice): a best effort, Studio may not show it for every channel."""
    name = "VIDEO_HAS_ALTERED_CONTENT_YES" if yes else "VIDEO_HAS_ALTERED_CONTENT_NO"
    try:
        radio = page.locator(f"tp-yt-paper-radio-button[name={name}]")
        if radio.count():
            radio.first.scroll_into_view_if_needed(timeout=10_000)
            radio.first.click(timeout=10_000)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _type_into(page, selector: str, text: str):
    box = page.locator(selector).first
    box.wait_for(state="visible", timeout=60_000)
    box.click()
    page.keyboard.press("Meta+A")
    page.keyboard.press("Backspace")
    # Line breaks in a contenteditable: Shift+Enter keeps them as new lines of the same field.
    for i, line in enumerate(text.split("\n")):
        if i:
            page.keyboard.press("Shift+Enter")
        if line:
            page.keyboard.insert_text(line)
    time.sleep(.4)


def _uploading(page) -> bool:
    """True while the file is still going up (the progress line shows a percentage of the upload)."""
    try:
        label = page.locator("ytcp-video-upload-progress .progress-label, ytcp-video-upload-progress span").first
        text = (label.inner_text(timeout=3000) or "").strip()
    except Exception:  # noqa: BLE001
        return False
    still = page.locator("ytcp-video-upload-progress[uploading], ytcp-video-upload-progress[is-uploading]")
    try:
        if still.count():
            return True
    except Exception:  # noqa: BLE001
        pass
    return bool(re.search(r"\d+\s?%", text)) and not re.search(r"(?i)process|обработ|verarbeit|procesa", text)


def upload(ws_endpoint: str, video: Path, meta: dict, thumb: Path | None, privacy: str = "public",
           log=lambda s: None, cancelled=lambda: False) -> str:
    """Uploads and publishes; returns the video link. meta: {title, description, tags, kids, category, altered}."""
    from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

    if not video.is_file():
        raise UserError(f"Нет файла видео: {video}")
    title = (meta.get("title") or video.stem)[:100].replace("<", "").replace(">", "")
    description = (meta.get("description") or "").replace("<", "").replace(">", "")[:5000]
    tags = ", ".join(t.replace(",", " ") for t in meta.get("tags") or [])[:490]
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(ws_endpoint, timeout=60_000)
        except Exception as e:  # noqa: BLE001
            raise UserError(f"Не получилось подключиться к профилю Octo: {e}")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        try:
            log("Открываю YouTube Studio…")
            page.goto(STUDIO, wait_until="domcontentloaded", timeout=120_000)
            time.sleep(4)
            if "accounts.google.com" in page.url or "ServiceLogin" in page.url:
                raise UserError("В этом профиле Octo не выполнен вход в YouTube. Откройте профиль, войдите в канал "
                                "и нажмите «Повторить выкладку».")
            page.goto(STUDIO.replace("studio.", "www.") + "/upload", wait_until="domcontentloaded", timeout=120_000)
            picker = page.locator("ytcp-uploads-file-picker input[type=file], input[type=file][name=Filedata]").first
            picker.wait_for(state="attached", timeout=90_000)
            log("Загружаю файл видео…")
            _set_file(page, picker, video)
            page.locator("#title-textarea #textbox").first.wait_for(state="visible", timeout=180_000)
            time.sleep(2)
            log("Вписываю название и описание…")
            _type_into(page, "#title-textarea #textbox", title)
            if description:
                _type_into(page, "#description-textarea #textbox", description)
            if thumb and thumb.is_file():
                log("Ставлю превью…")
                try:
                    _set_file(page, page.locator("input#file-loader[type=file], "
                                                 "ytcp-thumbnails-compact-editor-uploader input[type=file]").first, thumb)
                    time.sleep(4)
                except Exception as e:  # noqa: BLE001
                    raise UserError(f"YouTube не принял превью ({e}). Возможно, канал не подтверждён по телефону: "
                                    "без этого свои превью ставить нельзя.")
            kids = "VIDEO_MADE_FOR_KIDS_MFK" if meta.get("kids") else "VIDEO_MADE_FOR_KIDS_NOT_MFK"
            page.locator(f"tp-yt-paper-radio-button[name={kids}]").first.click()
            more: dict = {}
            if tags:
                log("Вписываю теги…")
                _expand(page, more)
                tag_box = page.locator("#tags-container input#text-input, ytcp-form-input-container#tags-container input").first
                tag_box.wait_for(state="visible", timeout=30_000)
                tag_box.click()
                tag_box.fill(tags + ",")
                time.sleep(1)
            cid = category_id(meta.get("category") or "")
            if cid:
                log("Ставлю категорию…")
                _expand(page, more)
                _set_category(page, cid, log)
            if meta.get("altered") is not None:
                _expand(page, more)
                _set_altered(page, bool(meta.get("altered")))
            for _ in range(3):
                if cancelled():
                    raise _Stop()
                nxt = page.locator("#next-button").first
                nxt.wait_for(state="visible", timeout=60_000)
                nxt.click()
                time.sleep(2)
            page.locator(f"tp-yt-paper-radio-button[name={PRIVACY.get(privacy, 'PUBLIC')}]").first.click()
            time.sleep(1)
            link = ""
            try:
                link = page.locator("ytcp-video-info a.ytcp-video-info, span.video-url-fadeable a").first.get_attribute(
                    "href", timeout=15_000) or ""
            except PWTimeout:
                link = ""
            log("Жду, пока файл загрузится целиком…")
            started = time.time()
            while _uploading(page):
                if cancelled():
                    raise _Stop()
                if time.time() - started > 6 * 3600:
                    raise UserError("Видео загружается дольше шести часов. Проверьте интернет в профиле Octo.")
                time.sleep(5)
            done = page.locator("#done-button").first
            for _ in range(120):
                if done.is_enabled():
                    break
                time.sleep(5)
            done.click()
            time.sleep(5)
            # Published while the file was still going up: Studio warns and keeps uploading; wait for it.
            for _ in range(720):
                if not page.locator("ytcp-uploads-still-processing-dialog, ytcp-video-upload-progress").count() or not _uploading(page):
                    break
                time.sleep(5)
            if not link:
                try:
                    link = page.locator("ytcp-video-share-dialog a, a#share-url").first.get_attribute("href", timeout=15_000) or ""
                except PWTimeout:
                    link = ""
            log("Опубликовано")
            return link
        except _Stop:
            raise UserError("Выкладка остановлена.")
        except PWTimeout as e:
            shot = video.parent / f".ошибка_выкладки_{int(time.time())}.png"
            try:
                page.screenshot(path=str(shot))
            except Exception:  # noqa: BLE001
                pass
            raise UserError(f"YouTube Studio не показал нужную кнопку вовремя ({str(e).splitlines()[0][:160]}). "
                            f"Снимок экрана: {shot}")
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
            # No browser.close(): leaving sync_playwright() only detaches, the Octo profile keeps running.
