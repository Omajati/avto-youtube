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
    handle = locator.element_handle(timeout=90_000) if hasattr(locator, "element_handle") else locator
    cdp = page.context.new_cdp_session(page)
    try:
        # A mark on the element is seen from every script world (a file-window handle may live in another one);
        # the search also walks into shadow roots.
        handle.evaluate("e => e.setAttribute('data-ay-file', '1')")
        obj = cdp.send("Runtime.evaluate", {"expression": """(() => {
            const walk = root => { const hit = root.querySelector('[data-ay-file]'); if (hit) return hit;
              for (const el of root.querySelectorAll('*')) if (el.shadowRoot) { const h = walk(el.shadowRoot); if (h) return h; }
              return null; };
            return walk(document); })()"""})
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
            handle.evaluate("e => e.removeAttribute('data-ay-file')")
            cdp.detach()
        except Exception:  # noqa: BLE001
            pass


PICKER = ("ytcp-uploads-file-picker input[type=file], input[type=file][name=Filedata], "
          "ytcp-uploads-dialog input[type=file]")
# Studio's new look (November 2026) has a labelled «Create» button; its inner names may change, so the buttons are
# also found by their text in the languages the owner's Studio may show.
CREATE_TEXT = r"^\s*(Create|Создать|Crear|Erstellen|Criar|Créer)\s*$"
UPLOAD_TEXT = r"(Upload videos?|Загрузить видео|Subir v[íi]deos?|Videos? hochladen|Carregar v[íi]deos?|Importer des v[ée]os)"
SELECT_TEXT = r"(Select files|Выбрать файлы|Seleccionar archivos|Dateien auswählen|Selecionar arquivos|Sélectionner des fichiers)"


UPLOAD_DIALOG = "ytcp-uploads-dialog"
POPUP = ("tp-yt-iron-dropdown:visible, tp-yt-paper-listbox:visible, ytcp-text-menu:visible, "
         "ytcp-date-picker tp-yt-paper-dialog:visible, tp-yt-paper-dialog:has(ytcp-date-picker):visible")
CANCEL_UPLOAD_TEXT = re.compile(r"(Cancel upload|Отменить загрузку|Отмена загрузки|Cancelar (la )?subida|Upload abbrechen|"
                                r"Hochladen abbrechen|Cancelar (o )?(upload|carregamento)|Annuler l.importation)", re.I)
KEEP_TEXT = re.compile(r"^\s*(Close|Закрыть|Cerrar|Schließen|Fechar|Fermer|Keep|Продолжить|Continue)\s*$", re.I)


def _upload_dialog_open(page) -> bool:
    try:
        return page.locator(UPLOAD_DIALOG).first.is_visible()
    except Exception:  # noqa: BLE001
        return False


def _keep_upload(page, log=lambda s: None) -> bool:
    """If Studio asks «Cancel upload? Any details you added will be deleted», answers «Close» (keep the upload).
    Such a question appears after a stray Escape or a click on the window's cross. True if it was answered."""
    try:
        dialogs = page.locator("ytcp-confirmation-dialog:visible, tp-yt-paper-dialog:visible, ytcp-dialog:visible")
        for i in range(min(dialogs.count(), 5)):
            d = dialogs.nth(i)
            if not CANCEL_UPLOAD_TEXT.search(d.inner_text(timeout=2000) or ""):
                continue
            keep = d.locator("#cancel-button, ytcp-button#cancel-button").first
            if not (keep.count() and keep.is_visible()):
                keep = d.get_by_role("button", name=KEEP_TEXT).first
            keep.click(timeout=5000)
            time.sleep(1)
            log("Studio спросил «Отменить загрузку?» — ответил «Закрыть», загрузка продолжается")
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _close_popup(page):
    """Escape only when a drop-down list or a calendar is really open: inside the upload window a stray Escape
    makes Studio ask «Cancel upload?»."""
    try:
        if page.locator(POPUP).count():
            page.keyboard.press("Escape")
            time.sleep(.5)
    except Exception:  # noqa: BLE001
        pass
    _keep_upload(page)


def _dismiss(page):
    """Closes the welcome / news / «what's new» windows Studio sometimes shows over everything. Never while the
    upload window is open: its own cross and Escape would cancel the upload."""
    if _upload_dialog_open(page):
        _keep_upload(page)
        return
    for sel in ("ytcp-warm-welcome-dialog #dismiss-button", "#dismiss-button", "ytcp-button#close-button",
                "#close-button", "tp-yt-paper-dialog #close-icon-button", "ytcp-dialog #close-button"):
        try:
            btn = page.locator(sel)
            if btn.count() and btn.first.is_visible():
                btn.first.click(timeout=3000)
                time.sleep(1)
        except Exception:  # noqa: BLE001
            pass
    if not _upload_dialog_open(page):
        try:
            page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass


def _picker_ready(page, wait_s: int):
    picker = page.locator(PICKER).first
    try:
        picker.wait_for(state="attached", timeout=wait_s * 1000)
        return picker
    except Exception:  # noqa: BLE001
        pass
    # Studio's new look may rename the upload window: any file field that accepts video will do.
    any_video = page.locator("input[type=file][accept*='video'], input[type=file][accept*='mp4']")
    try:
        if any_video.count():
            return any_video.first
    except Exception:  # noqa: BLE001
        pass
    return None


def _first_visible(locator, wait_s: float):
    end = time.time() + wait_s
    while time.time() < end:
        try:
            for i in range(min(locator.count(), 10)):
                if locator.nth(i).is_visible():
                    return locator.nth(i)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(.5)
    return None


def _click_create_upload(page) -> bool:
    """«Create» → «Upload videos»: by the old inner names first, then by the visible text."""
    import re as _re
    create = _first_visible(page.locator("ytcp-button#create-icon, #create-icon"), 4) or _first_visible(
        page.get_by_role("button", name=_re.compile(CREATE_TEXT, _re.I)), 10) or _first_visible(
        page.locator("button, ytcp-button, [role=button]").filter(has_text=_re.compile(CREATE_TEXT, _re.I)), 5)
    if not create:
        return False
    try:
        create.click(timeout=10_000)
    except Exception:  # noqa: BLE001
        return False
    time.sleep(1)
    item = _first_visible(page.locator("tp-yt-paper-item#text-item-0, #text-item-0"), 3) or _first_visible(
        page.get_by_role("menuitem", name=_re.compile(UPLOAD_TEXT, _re.I)), 5) or _first_visible(
        page.locator("tp-yt-paper-item, [role=menuitem], [role=option], li, a, button").filter(
            has_text=_re.compile(UPLOAD_TEXT, _re.I)), 5) or _first_visible(
        page.get_by_text(_re.compile(r"^\s*" + UPLOAD_TEXT + r"\s*$", _re.I)), 3)
    if not item:
        return False
    try:
        item.click(timeout=10_000)
        return True
    except Exception:  # noqa: BLE001
        return False


def _picker_via_chooser(page):
    """Last resort: press «Select files» and take the file field the browser's file window was opened for."""
    import re as _re
    btn = _first_visible(page.locator("#select-files-button, ytcp-button#select-files-button"), 2) or _first_visible(
        page.get_by_role("button", name=_re.compile(SELECT_TEXT, _re.I)), 5)
    if not btn:
        return None
    try:
        with page.expect_file_chooser(timeout=15_000) as info:
            btn.click(timeout=10_000)
        return info.value.element
    except Exception:  # noqa: BLE001
        return None


def _open_upload(page, video: Path, log):
    """Opens Studio's upload window and returns its file field: first the «Создать → Загрузить видео» button in
    Studio (the windows that may cover the page are closed first), the /upload address only as a spare way."""
    if "studio.youtube.com" not in page.url:
        page.goto(STUDIO, wait_until="domcontentloaded", timeout=120_000)
        time.sleep(5)
    _check_url(page)
    log("Нажимаю «Создать → Загрузить видео»…")
    for attempt in range(3):
        _dismiss(page)
        clicked = _click_create_upload(page)
        if not clicked:
            for sel in ("ytcp-quick-actions #upload-icon", "#upload-icon"):
                try:
                    btn = page.locator(sel)
                    if btn.count() and btn.first.is_visible():
                        btn.first.click(timeout=5000)
                        break
                except Exception:  # noqa: BLE001
                    pass
        picker = _picker_ready(page, 20)
        if picker:
            return picker
        picker = _picker_via_chooser(page)
        if picker:
            return picker
    log("Кнопка не открыла окно загрузки — пробую адрес youtube.com/upload…")
    page.goto(STUDIO.replace("studio.", "www.") + "/upload", wait_until="domcontentloaded", timeout=120_000)
    picker = _picker_ready(page, 40)
    if picker:
        return picker
    _check_url(page)
    shot = video.parent / f".ошибка_выкладки_{int(time.time())}.png"
    try:
        page.screenshot(path=str(shot))
    except Exception:  # noqa: BLE001
        pass
    raise UserError(f"YouTube Studio не открыл окно загрузки видео (страница: {page.url[:120]}). Откройте этот профиль "
                    f"Octo руками и посмотрите, что на экране: выбор канала, подтверждение, окно приветствия. "
                    f"Снимок экрана: {shot}")


def _check_url(page):
    url = page.url
    if "accounts.google.com" in url or "ServiceLogin" in url:
        raise UserError("В этом профиле Octo не выполнен вход в YouTube (или Google просит войти заново). Откройте "
                        "профиль, войдите в канал и нажмите «Повторить выкладку».")
    if "channel_switcher" in url or "select_site" in url or "/channel_switcher" in url:
        raise UserError("YouTube просит выбрать канал: в этом аккаунте Google их несколько. Откройте профиль Octo, "
                        "выберите нужный канал (он запомнится) и нажмите «Повторить выкладку».")


SHOW_MORE_TEXT = r"^\s*(Show more|Show advanced settings|Развернуть|Показать больше|Ещё|Mostrar más|Mehr anzeigen|Mostrar mais|Afficher plus)\s*$"
TAGS_BOX = "#tags-container input#text-input, ytcp-form-input-container#tags-container input, #tags-container input"


def _more_open(page) -> bool:
    for sel in (TAGS_BOX, "ytcp-form-select#category, #category ytcp-dropdown-trigger"):
        try:
            if _first_visible(page.locator(sel), 1.5):
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


def _expand(page, state: dict, log=lambda s: None) -> bool:
    """Opens «Show more» (tags, category and the AI label live there). Several elements on the page may be named
    #toggle-button, so the visible one is taken, scrolled to and pressed; then the tags field must show up. Never
    waits forever: if the section does not open, False and the steps that need it are skipped."""
    if state.get("open"):
        return True
    if _more_open(page):
        state["open"] = True
        return True
    for attempt in range(3):
        btn = _first_visible(page.locator("ytcp-uploads-dialog ytcp-button#toggle-button, ytcp-uploads-dialog #toggle-button, "
                                          "ytcp-button#toggle-button, #toggle-button"), 5) or _first_visible(
            page.get_by_role("button", name=re.compile(SHOW_MORE_TEXT, re.I)), 3) or _first_visible(
            page.locator("ytcp-button, button, [role=button]").filter(has_text=re.compile(SHOW_MORE_TEXT, re.I)), 3)
        if btn:
            try:
                btn.scroll_into_view_if_needed(timeout=10_000)
                if attempt < 2:
                    btn.click(timeout=10_000)
                else:
                    btn.evaluate("e => e.click()")  # something covers the button: press it from the page itself
            except Exception:  # noqa: BLE001
                try:
                    btn.evaluate("e => e.click()")
                except Exception:  # noqa: BLE001
                    pass
        else:
            # The button may sit below the visible part of the window: scroll the dialog down and look again.
            try:
                page.locator("ytcp-uploads-dialog #scrollable-content, ytcp-uploads-dialog .scrollable").first.evaluate(
                    "e => e.scrollTop = e.scrollHeight")
            except Exception:  # noqa: BLE001
                pass
        time.sleep(1.5)
        if _more_open(page):
            state["open"] = True
            return True
    log("Не смог развернуть «Показать больше» — теги и категорию пропускаю, выкладка продолжается")
    state["open"] = False
    state["failed"] = True
    return False


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
        _close_popup(page)
    except Exception as e:  # noqa: BLE001
        _close_popup(page)
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


def _clear(page, box):
    """Empties a Studio text box. YouTube pre-fills the title with the file name; Cmd+A sent through the debug
    port does not always select everything on a Mac, so the text is also selected and removed by the page itself."""
    box.click()
    for keys in ("ControlOrMeta+A", "Meta+A", "Control+A"):
        try:
            page.keyboard.press(keys)
            page.keyboard.press("Backspace")
        except Exception:  # noqa: BLE001
            pass
        if not (box.inner_text() or "").strip():
            return
    box.evaluate("""e => { e.focus(); const r = document.createRange(); r.selectNodeContents(e);
        const s = window.getSelection(); s.removeAllRanges(); s.addRange(r); }""")
    page.keyboard.press("Delete")
    if (box.inner_text() or "").strip():
        box.evaluate("e => { e.textContent = ''; e.dispatchEvent(new InputEvent('input', {bubbles: true})); }")


def _norm(t: str) -> str:
    return " ".join((t or "").split())


def _type_into(page, selector: str, text: str):
    box = page.locator(selector).first
    box.wait_for(state="visible", timeout=60_000)
    for attempt in range(3):
        _clear(page, box)
        # Line breaks in a contenteditable: Shift+Enter keeps them as new lines of the same field.
        for i, line in enumerate(text.split("\n")):
            if i:
                page.keyboard.press("Shift+Enter")
            if line:
                page.keyboard.insert_text(line)
        time.sleep(.6)
        if _norm(box.inner_text()) == _norm(text):
            return
    raise UserError(f"Не получилось вписать текст в поле YouTube Studio: там «{_norm(box.inner_text())[:80]}».")


# No inline (?i): the pattern is also run inside the browser, where JavaScript does not know it; re.I is passed on.
UPLOADING_TEXT = re.compile(r"(Uploading|Загрузка|Загружено|Загружается|Subiendo|Wird hochgeladen|Hochladen|"
                            r"Enviando|Carregando|Importation|Mise en ligne)\D{0,8}(\d{1,3})\s?%", re.I)
# YouTube's checks (copyright and the rules), shown in the same status line once the file is up.
CHECKS_OK = re.compile(r"(No issues found|(Проблем|Нарушений) не (обнаружено|найдено|выявлено)|"
                       r"Не (обнаружено|найдено|выявлено) (проблем|нарушений)|No se ha encontrado ning[uú]n problema|"
                       r"No se (han )?encontrado problemas|No se encontraron problemas|Keine Probleme gefunden|"
                       r"Keine Verst[öo](ß|ss)e gefunden|Nenhum problema encontrado|Aucun probl[eè]me d[ée]tect[ée])", re.I)
# Any other wording of «nothing was found» («Нарушений не найдено»): a «no/не/keine» before the «found» word, on a line
# that also names issues/violations/copyright (so an unrelated «nothing found» elsewhere on the page does not count).
CHECKS_NONE = re.compile(r"\b(no|not|не|keine?|nenhum|aucun|ning[uú]n)\b[^.!\n]{0,25}"
                         r"(found|detected|найден|обнаружен|выявлен|encontr|gefunden|erkannt|d[ée]tect)", re.I)
CHECKS_BAD = re.compile(r"(Copyright|авторск|derechos de autor|Urheberrecht|direitos autorais|droits d.auteur|"
                        r"issues?|problems?|проблем|нарушени|problema|Probleme?|violation|infracci|Versto)", re.I)
# …and the same line says something was found: so a «privacy or copyright» link text is not taken for a verdict.
CHECKS_FOUND = re.compile(r"(found|detected|обнаруж|найден|выявлен|encontr|detect|gefunden|erkannt|encontrad|d[ée]tect)",
                          re.I)


# Studio's own words that the file is up: only after one of them (or a whole minute without any upload percentage)
# the upload counts as finished.
UPLOAD_DONE = re.compile(r"(Upload complete|Uploaded|Загрузка завершена|Видео загружено|Загружено полностью|"
                         r"Subida completada|Se ha subido|Hochladen abgeschlossen|Upload abgeschlossen|Carregamento conclu|"
                         r"Importation termin|Processing|Обработка|Обрабатывается|Procesando|Verarbeitung|Wird verarbeitet|"
                         r"Processando|Traitement|Checks complete|Проверка завершена|Проверки завершены|Comprobaciones completadas|"
                         r"Überprüfungen abgeschlossen|Checking|Проверка|Comprobando|Wird überprüft)", re.I)


def _wait_upload(page, log, cancelled, max_s: int = 6 * 3600):
    """Waits until the file is fully up: Studio says so in words, or no upload percentage for a whole minute."""
    log("Жду, пока файл загрузится целиком…")
    started, shown, quiet = time.time(), None, 0
    time.sleep(3)
    while True:
        if cancelled():
            raise _Stop()
        if time.time() - started > max_s:
            raise UserError("Видео загружается дольше шести часов. Ролик НЕ опубликован. Проверьте интернет в профиле Octo.")
        _keep_upload(page, log)
        pct = _upload_percent(page)
        if pct is not None or _uploading(page):
            quiet = 0
            if pct is not None and pct != shown:
                shown = pct
                log(f"Файл загружается на YouTube: {pct}%")
        else:
            quiet += 1
            if UPLOAD_DONE.search(_status_text(page)) and quiet >= 2:
                log("Файл загружен целиком")
                return
            if quiet >= 12:  # a whole minute with no upload going on
                log("Файл загружен (загрузка больше не идёт)")
                return
        time.sleep(5)


def _status_text(page) -> str:
    """All the short status lines visible in the upload window (the footer and the Checks step)."""
    try:
        return page.evaluate("""() => {
            const out = []; const walk = root => { for (const el of root.querySelectorAll('*')) {
              if (el.shadowRoot) walk(el.shadowRoot);
              if (el.children.length === 0 && el.offsetParent !== null) { const t = (el.textContent || '').trim();
                if (t && t.length < 200) out.push(t); } } };
            walk(document); return out.join('\\n'); }""") or ""
    except Exception:  # noqa: BLE001
        return ""


def _wait_checks(page, log, cancelled, max_s: int = 3 * 3600):
    """After the upload: waits until YouTube has finished its checks. «No issues found», seen twice 10 s apart with
    no upload going on -> True. A copyright or rules problem, or no verdict in time -> UserError: the video is NOT
    published or scheduled, it stays a draft in Studio for the owner to decide."""
    log("Жду проверку YouTube (авторские права и правила)…")
    started, last_log, ok_seen = time.time(), 0, 0
    while time.time() - started < max_s:
        if cancelled():
            raise _Stop()
        _keep_upload(page, log)
        text = _status_text(page)
        line = next((l for l in text.splitlines() if CHECKS_BAD.search(l) and CHECKS_FOUND.search(l)
                     and not CHECKS_OK.search(l) and not CHECKS_NONE.search(l)), "")
        if line:
            raise UserError(f"YouTube при проверке нашёл проблему: «{line[:160]}». Ролик загружен, но НЕ опубликован "
                            "(лежит черновиком в Studio): посмотрите шаг «Checks» и решите сами.")
        good = CHECKS_OK.search(text) or any(CHECKS_BAD.search(l) and CHECKS_NONE.search(l) for l in text.splitlines())
        if good and not _uploading(page):
            ok_seen += 1
            if ok_seen >= 2:
                said = next((l for l in text.splitlines() if CHECKS_OK.search(l) or
                             (CHECKS_BAD.search(l) and CHECKS_NONE.search(l))), "")
                log(f"Проверка YouTube пройдена: «{said[:80]}»")
                return True
        else:
            ok_seen = 0
        if time.time() - last_log > 300:
            last_log = time.time()
            log(f"Жду проверку YouTube… ({int((time.time() - started) // 60)} мин)")
        time.sleep(10)
    raise UserError("YouTube не закончил проверку за 3 часа. Ролик загружен, но НЕ опубликован и не запланирован "
                    "(лежит черновиком в Studio): когда проверка закончится, опубликуйте его там сами.")


def _upload_percent(page):
    """The upload percentage Studio shows at the bottom of the window («Uploading 10% ... 4 minutes left»), or None.
    Found by the text itself, so a new look of Studio with renamed elements does not fool it."""
    try:
        hits = page.get_by_text(UPLOADING_TEXT)
        for i in range(min(hits.count(), 5)):
            if hits.nth(i).is_visible():
                m = UPLOADING_TEXT.search(hits.nth(i).inner_text(timeout=3000) or "")
                if m:
                    return int(m.group(2))
    except Exception:  # noqa: BLE001
        pass
    return None


def _uploading(page) -> bool:
    """True while the file is still going up."""
    if _upload_percent(page) is not None:
        return True
    try:
        if page.locator("ytcp-video-upload-progress[uploading], ytcp-video-upload-progress[is-uploading]").count():
            return True
        label = page.locator("ytcp-video-upload-progress .progress-label, ytcp-video-upload-progress span").first
        text = (label.inner_text(timeout=3000) or "").strip()
    except Exception:  # noqa: BLE001
        return False
    return bool(re.search(r"\d+\s?%", text)) and not re.search(r"(?i)process|обработ|verarbeit|procesa", text)


PUBLISH_TEXT = (r"^\s*(Publish|Опубликовать|Publicar|Veröffentlichen|Save|Сохранить|Guardar|Speichern|"
                r"Schedule|Запланировать|Programar|Planen|Agendar|Programmer)\s*$")
SCHEDULE_TEXT = r"(Schedule|Запланировать|Programar|Planen|Agendar|Programmer)"
GMT = re.compile(r"(?:GMT|UTC)\s?([+\-−–])\s?(\d{1,2})(?::?(\d{2}))?")


def _studio_zone(page):
    """The time zone Studio's schedule fields are in: the «GMT+03:00» it shows next to them, else the browser's own
    (an Octo profile has the zone of its proxy). Returns a tzinfo and its name for the log."""
    from datetime import timedelta, timezone
    for line in _status_text(page).splitlines():
        m = GMT.search(line)
        if m and len(line) < 80:
            mins = int(m.group(2)) * 60 + int(m.group(3) or 0)
            sign = -1 if m.group(1) in "-−–" else 1
            return timezone(timedelta(minutes=sign * mins)), m.group(0)
    try:
        from zoneinfo import ZoneInfo
        name = page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone") or ""
        if name:
            return ZoneInfo(name), name
    except Exception:  # noqa: BLE001
        pass
    return None, ""


def _page_lang(page) -> str:
    try:
        return page.evaluate("document.documentElement.lang || navigator.language") or "en"
    except Exception:  # noqa: BLE001
        return "en"


def _date_ok(shown: str, day: int, month: int, year: int, months: list[str]) -> bool:
    low = (shown or "").lower()
    nums = [int(x) for x in re.findall(r"\d+", low)]
    if day not in nums or year not in nums:
        return False
    return month in [n for n in nums if n not in (year,)] or any(m and m in low for m in months)


def _set_schedule(page, when, log) -> bool:
    """«Schedule»: opens the block on the visibility step, types the date and the time (in the formats of Studio's
    own language, made by the browser) and reads them back. False if Studio did not take them."""
    opener = _first_visible(page.locator("#second-container-expand-button, ytcp-icon-button#second-container-expand-button, "
                                         "tp-yt-paper-radio-button#schedule-radio-button"), 5) or _first_visible(
        page.locator("#second-container").filter(has_text=re.compile(SCHEDULE_TEXT, re.I)), 3) or _first_visible(
        page.get_by_text(re.compile(r"^\s*" + SCHEDULE_TEXT, re.I)), 3)
    trigger_sel = "#datepicker-trigger, ytcp-text-dropdown-trigger#datepicker-trigger, ytcp-date-picker ytcp-dropdown-trigger"
    if not _first_visible(page.locator(trigger_sel), 1):
        if not opener:
            log("Не нашёл блок «Запланировать публикацию»")
            return False
        try:
            opener.scroll_into_view_if_needed(timeout=10_000)
            opener.click(timeout=10_000)
        except Exception:  # noqa: BLE001
            try:
                opener.evaluate("e => e.click()")
            except Exception:  # noqa: BLE001
                pass
        time.sleep(1.5)
    trigger = _first_visible(page.locator(trigger_sel), 10)
    if not trigger:
        log("Блок «Запланировать» не открылся")
        return False
    zone, zone_name = _studio_zone(page)
    local = when.astimezone(zone) if zone else when
    lang = _page_lang(page)
    fmt = page.evaluate("""([y, m, d, hh, mm, lang]) => { const dt = new Date(y, m - 1, d, hh, mm); const out = {dates: [], times: [], months: []};
        const add = (arr, f) => { try { const v = f(); if (v && !arr.includes(v)) arr.push(v); } catch (e) {} };
        for (const o of [{dateStyle: 'medium'}, {dateStyle: 'short'}, {year: 'numeric', month: '2-digit', day: '2-digit'}, {dateStyle: 'long'}])
          add(out.dates, () => new Intl.DateTimeFormat(lang, o).format(dt));
        add(out.dates, () => new Intl.DateTimeFormat('en-US', {dateStyle: 'medium'}).format(dt));
        for (const o of [{timeStyle: 'short'}, {hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}, {hour: 'numeric', minute: '2-digit', hour12: true}])
          add(out.times, () => new Intl.DateTimeFormat(lang, o).format(dt).replace(/\u202f|\u00a0/g, ' '));
        add(out.times, () => new Intl.DateTimeFormat('en-US', {hour: 'numeric', minute: '2-digit', hour12: true}).format(dt).replace(/\u202f|\u00a0/g, ' '));
        for (const o of [{month: 'long'}, {month: 'short'}]) add(out.months, () => new Intl.DateTimeFormat(lang, o).format(dt).toLowerCase().replace(/[.]$/, ''));
        return out; }""", [local.year, local.month, local.day, local.hour, local.minute, lang])
    date_ok = False
    for text in fmt.get("dates") or []:
        try:
            trigger.click(timeout=10_000)
            time.sleep(1)
            box = _first_visible(page.locator("ytcp-date-picker tp-yt-paper-input input, ytcp-date-picker input, "
                                              "tp-yt-paper-dialog ytcp-date-picker input, #datepicker input"), 5)
            if not box:
                continue
            box.fill(text, timeout=10_000)
            box.press("Enter")
            time.sleep(1.2)
            shown = trigger.inner_text(timeout=5000)
            if _date_ok(shown, local.day, local.month, local.year, fmt.get("months") or []):
                date_ok = True
                break
        except Exception:  # noqa: BLE001
            pass
        _close_popup(page)
    if not date_ok:
        log("Studio не принял дату публикации")
        return False
    time_ok = False
    h12 = local.hour % 12 or 12
    for text in fmt.get("times") or []:
        try:
            box = _first_visible(page.locator("#time-of-day-container input, ytcp-form-input-container#time-of-day-container input, "
                                              "#time-of-day-trigger input, ytcp-visibility-scheduler input"), 5)
            if not box:
                break
            box.click(timeout=10_000)
            box.fill(text, timeout=10_000)
            box.press("Enter")
            time.sleep(1)
            nums = [int(x) for x in re.findall(r"\d+", box.input_value(timeout=5000) or "")]
            if len(nums) >= 2 and nums[0] in (local.hour, h12) and nums[1] == local.minute:
                time_ok = True
                break
        except Exception:  # noqa: BLE001
            pass
    _close_popup(page)  # a list of times may stay open over the buttons
    if not time_ok:
        log("Studio не принял время публикации")
        return False
    log(f"Публикация запланирована: {local.strftime('%d.%m.%Y %H:%M')} по времени Studio ({zone_name or 'пояс браузера'})")
    return True


def _publish_button(page):
    btn = page.locator("#done-button")
    try:
        if btn.count() and btn.first.is_visible():
            return btn.first
    except Exception:  # noqa: BLE001
        pass
    hits = page.get_by_role("button", name=re.compile(PUBLISH_TEXT, re.I))
    try:
        for i in range(min(hits.count(), 5)):
            if hits.nth(i).is_visible():
                return hits.nth(i)
    except Exception:  # noqa: BLE001
        pass
    return btn.first


CHANNEL_ID = re.compile(r"\b(UC[A-Za-z0-9_-]{22})\b")


def channel_ref_of(text: str) -> str:
    """What the owner typed for the channel: a link /channel/UC…, a bare UC… id, @handle or a youtube.com/@handle
    link -> 'UC…' or '@handle'; '' when it is neither."""
    text = (text or "").strip()
    m = CHANNEL_ID.search(text)
    if m:
        return m.group(1)
    m = re.search(r"(?:^|/)(@[\w.\-À-ɏЀ-ӿ]{2,})", text)
    return m.group(1) if m else ""


def _resolve_channel(context, page, ref: str, log) -> str:
    """The UC… id of the channel the video must go to.
    1) The one the owner set for this channel (an id, or a @handle looked up on its own YouTube page).
    2) Not set: the channel already open in the profile's own Studio tab, else the one this profile has selected on
       youtube.com (its «Advanced settings» page shows the channel id). Studio's bare address may open ANOTHER
       channel of the same Google account (the one used in Studio last), so the address always names the channel."""
    ref = channel_ref_of(ref)
    if ref.startswith("UC"):
        return ref
    if ref.startswith("@"):
        try:
            page.goto(f"https://www.youtube.com/{ref}", wait_until="domcontentloaded", timeout=120_000)
            time.sleep(2)
            href = page.locator("link[rel=canonical]").first.get_attribute("href", timeout=10_000) or ""
            m = CHANNEL_ID.search(href) or CHANNEL_ID.search(page.content())
            if m:
                return m.group(1)
        except Exception:  # noqa: BLE001
            pass
        raise UserError(f"Не нашёл на YouTube канал {ref}. Проверьте адрес канала в настройках канала программы "
                        "(лучше вставить ссылку вида youtube.com/channel/UC…).")
    for other in list(context.pages):
        if other is page:
            continue
        m = re.search(r"studio\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})", other.url or "")
        if m:
            log("Канал беру из открытой в профиле вкладки Studio")
            return m.group(1)
    try:
        page.goto("https://www.youtube.com/account_advanced", wait_until="domcontentloaded", timeout=120_000)
        time.sleep(2)
        m = CHANNEL_ID.search(page.locator("body").inner_text(timeout=15_000) or "")
        if m:
            log("Канал беру тот, что выбран в профиле на YouTube")
            return m.group(1)
    except Exception:  # noqa: BLE001
        pass
    log("Не смог узнать, какой канал выбран в профиле, — Studio откроет свой последний")
    return ""


def _studio_channel(page) -> tuple[str, str]:
    """(UC… id from Studio's address, the channel name in its left menu)."""
    m = re.search(r"/channel/(UC[A-Za-z0-9_-]{22})", page.url or "")
    name = ""
    try:
        box = _first_visible(page.locator("ytcp-navigation-drawer #entity-name, #entity-name, .entity-name"), 5)
        name = _norm(box.inner_text(timeout=3000)) if box else ""
    except Exception:  # noqa: BLE001
        pass
    return (m.group(1) if m else ""), name


def _check_channel(page, want: str, log, quiet: bool = False) -> str:
    """Stops BEFORE anything goes to the wrong channel: Studio must be on the wanted one. Returns its name."""
    have, name = _studio_channel(page)
    if want and have and have != want:
        raise UserError(f"YouTube Studio открыл другой канал{f' «{name}»' if name else ''} ({have}) вместо нужного "
                        f"({want}) — НЕ выкладываю. Откройте профиль Octo, переключитесь на нужный канал "
                        "и нажмите «Повторить выкладку».")
    if not quiet:
        log(f"Канал в Studio: «{name or '?'}» ({have or want or 'адрес без номера канала'})")
    return name


def _thumb_imgs(page) -> list[str]:
    """src of the pictures inside Studio's thumbnail block (a set thumbnail shows up there as data:/blob: image)."""
    try:
        return page.evaluate("""() => { const out = []; const walk = root => { for (const el of root.querySelectorAll('*')) {
              if (el.shadowRoot) walk(el.shadowRoot);
              if (el.tagName === 'IMG' && el.offsetParent !== null) { let p = el, hit = false;
                for (let i = 0; p && i < 12; i++, p = p.parentElement || (p.getRootNode && p.getRootNode().host))
                  if (/THUMBNAIL/i.test(p.tagName || '') || /thumbnail/i.test(p.id || '')) { hit = true; break; }
                if (hit && el.src) out.push(el.src); } } };
            walk(document); return out; }""") or []
    except Exception:  # noqa: BLE001
        return []


THUMB_INPUT = ("ytcp-thumbnails-compact-editor-uploader input[type=file], ytcp-thumbnail-uploader input[type=file], "
               "[id*=thumbnail] input[type=file], input#file-loader[type=file][accept*=image], "
               "input[type=file][accept*='image/jpeg'], input[type=file][accept*='image/png']")
THUMB_BUTTON = r"(Upload (file|thumbnail)|Загрузить (файл|значок|превью)|Subir (archivo|miniatura)|Datei hochladen|Carregar arquivo|Importer un fichier)"


def _thumb_set(page, before: list[str], wait_s: float = 25) -> bool:
    end = time.time() + wait_s
    while time.time() < end:
        now = _thumb_imgs(page)
        if any(src.startswith(("data:", "blob:")) and src not in before for src in now):
            return True
        time.sleep(1)
    return False


def _set_thumb(page, thumb: Path, log) -> bool:
    """Puts the thumbnail and makes sure Studio shows it. First the image file field of the thumbnail block (never a
    video field: those also exist on the page), then the block's «Upload file» button with the browser's file window."""
    before = _thumb_imgs(page)
    try:
        field = None
        loc = page.locator(THUMB_INPUT)
        for i in range(min(loc.count(), 10)):
            acc = (loc.nth(i).get_attribute("accept", timeout=3000) or "").lower()
            if "video" not in acc:
                field = loc.nth(i)
                break
        if field is not None:
            _set_file(page, field, thumb)
            if _thumb_set(page, before):
                log("Превью поставлено")
                return True
    except Exception as e:  # noqa: BLE001
        log(f"Поле превью не сработало ({str(e).splitlines()[0][:100]}) — пробую кнопкой")
    try:
        btn = _first_visible(page.locator("ytcp-thumbnails-compact-editor-uploader, #select-button, "
                                          "ytcp-thumbnail-uploader button"), 3) or _first_visible(
            page.get_by_role("button", name=re.compile(THUMB_BUTTON, re.I)), 3) or _first_visible(
            page.locator("button, ytcp-button, [role=button]").filter(has_text=re.compile(THUMB_BUTTON, re.I)), 3)
        if btn:
            btn.scroll_into_view_if_needed(timeout=10_000)
            with page.expect_file_chooser(timeout=15_000) as info:
                btn.click(timeout=10_000)
            info.value.set_files(str(thumb)) if thumb.stat().st_size < 45 * 1024 * 1024 else _set_file(
                page, info.value.element, thumb)
            if _thumb_set(page, before):
                log("Превью поставлено")
                return True
    except Exception as e:  # noqa: BLE001
        log(f"Кнопка превью не сработала ({str(e).splitlines()[0][:100]})")
    return False


def _thumb_on_edit_page(page, link: str, thumb: Path, log) -> bool:
    """Second try after publishing: the video's own page in Studio, thumbnail, «Save»."""
    vid = video_id(link)
    if not vid:
        log("ПРЕВЬЮ НЕ ПОСТАВЛЕНО: нет ссылки на ролик. Поставьте его руками в Studio")
        return False
    try:
        page.goto(f"{STUDIO}/video/{vid}/edit", wait_until="domcontentloaded", timeout=120_000)
        page.locator("#title-textarea #textbox").first.wait_for(state="visible", timeout=60_000)
        time.sleep(2)
        if _set_thumb(page, thumb, log):
            save = _first_visible(page.locator("ytcp-button#save, #save"), 10)
            if save:
                for _ in range(30):
                    if save.is_enabled():
                        break
                    time.sleep(1)
                save.click(timeout=10_000)
                time.sleep(5)
                log("Превью сохранено на странице ролика")
                return True
    except Exception as e:  # noqa: BLE001
        log(f"Страница ролика: {str(e).splitlines()[0][:100]}")
    log("ПРЕВЬЮ НЕ ПОСТАВЛЕНО — поставьте его руками в Studio (файл превью лежит в папке ролика)")
    return False


def video_id(link: str) -> str:
    m = re.search(r"(?:youtu\.be/|[?&]v=|/video/|/shorts/)([A-Za-z0-9_-]{11})", link or "")
    return m.group(1) if m else ""


def _verify(page, link: str, title: str, log) -> bool:
    """After publishing: opens the video's own page in Studio again and checks it is there with our title.
    Only then the finished MP4 may go to the Trash."""
    vid = video_id(link)
    if not vid:
        log("Ссылку на ролик не нашёл — не могу перепроверить, MP4 оставляю")
        return False
    log("Перепроверяю, что ролик есть на канале…")
    for attempt in range(3):
        try:
            page.goto(f"{STUDIO}/video/{vid}/edit", wait_until="domcontentloaded", timeout=120_000)
            box = page.locator("#title-textarea #textbox").first
            box.wait_for(state="visible", timeout=60_000)
            time.sleep(2)
            if _norm(box.inner_text()) == _norm(title):
                log("Ролик на канале, название совпадает")
                return True
            log(f"На странице ролика другое название: «{_norm(box.inner_text())[:60]}» — MP4 оставляю")
            return False
        except Exception:  # noqa: BLE001
            time.sleep(10)
    log("Страница ролика в Studio не открылась — MP4 оставляю")
    return False


def upload(ws_endpoint: str, video: Path, meta: dict, thumb: Path | None, privacy: str = "public",
           log=lambda s: None, cancelled=lambda: False, result: dict | None = None, channel_ref: str = "",
           schedule=None) -> str:
    """Uploads and publishes; returns the video link. meta: {title, description, tags, kids, category, altered}.
    channel_ref: the YouTube channel it must go to (UC… id, a link or @handle); empty = the one open in the profile.
    schedule: an aware datetime — the video goes up now and YouTube itself makes it public then.
    result gets video_id, verified, thumb_ok, channel_id, channel_name and scheduled (True/False/None)."""
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
            want = _resolve_channel(context, page, channel_ref, log)
            log("Открываю YouTube Studio…")
            page.goto(f"{STUDIO}/channel/{want}" if want else STUDIO, wait_until="domcontentloaded", timeout=120_000)
            time.sleep(4)
            if "accounts.google.com" in page.url or "ServiceLogin" in page.url:
                raise UserError("В этом профиле Octo не выполнен вход в YouTube. Откройте профиль, войдите в канал "
                                "и нажмите «Повторить выкладку».")
            channel_name = _check_channel(page, want, log)
            picker = _open_upload(page, video, log)
            _check_channel(page, want, log, quiet=True)
            log("Загружаю файл видео…")
            _set_file(page, picker, video)
            page.locator("#title-textarea #textbox").first.wait_for(state="visible", timeout=180_000)
            time.sleep(2)
            log("Вписываю название и описание…")
            _type_into(page, "#title-textarea #textbox", title)
            if description:
                _type_into(page, "#description-textarea #textbox", description)
            thumb_ok = None
            if thumb and thumb.is_file():
                log("Ставлю превью…")
                thumb_ok = _set_thumb(page, thumb, log)
                if not thumb_ok:
                    log("Превью в окне загрузки не встало — поставлю его на странице ролика после публикации")
            kids = "VIDEO_MADE_FOR_KIDS_MFK" if meta.get("kids") else "VIDEO_MADE_FOR_KIDS_NOT_MFK"
            page.locator(f"tp-yt-paper-radio-button[name={kids}]").first.click()
            more: dict = {}
            if tags:
                log("Вписываю теги…")
                if _expand(page, more, log):
                    try:
                        tag_box = _first_visible(page.locator(TAGS_BOX), 20)
                        if not tag_box:
                            raise RuntimeError("поле тегов не появилось")
                        tag_box.scroll_into_view_if_needed(timeout=10_000)
                        tag_box.click(timeout=10_000)
                        tag_box.fill(tags + ",")
                        time.sleep(1)
                    except Exception as e:  # noqa: BLE001
                        log(f"Теги вписать не получилось ({str(e).splitlines()[0][:100]}) — продолжаю без них")
            cid = category_id(meta.get("category") or "")
            if cid and not more.get("failed"):
                log("Ставлю категорию…")
                if _expand(page, more, log):
                    _set_category(page, cid, log)
            if meta.get("altered") is not None and not more.get("failed"):
                if _expand(page, more, log):
                    _set_altered(page, bool(meta.get("altered")))
            for _ in range(3):
                if cancelled():
                    raise _Stop()
                _close_popup(page)
                _keep_upload(page, log)
                nxt = page.locator("#next-button").first
                nxt.wait_for(state="visible", timeout=60_000)
                nxt.click()
                time.sleep(2)
            _keep_upload(page, log)
            scheduled = None
            if schedule is not None:
                log("Ставлю отложенную публикацию…")
                scheduled = _set_schedule(page, schedule, log)
                if not scheduled:
                    # Never public before its time: the video is kept hidden, the owner sets the date in Studio.
                    log("Дату поставить не получилось — сохраняю ролик скрытым (Ограниченный доступ)")
                    page.locator("tp-yt-paper-radio-button[name=PRIVATE]").first.click()
            else:
                page.locator(f"tp-yt-paper-radio-button[name={PRIVACY.get(privacy, 'PUBLIC')}]").first.click()
            time.sleep(1)
            link = ""
            try:
                link = page.locator("ytcp-video-info a.ytcp-video-info, span.video-url-fadeable a").first.get_attribute(
                    "href", timeout=15_000) or ""
            except PWTimeout:
                link = ""
            # The Publish / Schedule button is pressed only after BOTH: the file is fully up and YouTube's checks
            # said «no issues». Any doubt -> an error, and the video stays an unpublished draft.
            _wait_upload(page, log, cancelled)
            _wait_checks(page, log, cancelled)
            if _uploading(page):
                raise UserError("Studio снова показывает загрузку файла — ролик НЕ опубликован. Нажмите «Повторить».")
            _keep_upload(page, log)
            _check_channel(page, want, log, quiet=True)
            log("Все проверки пройдены. " + ("Ставлю в расписание…" if scheduled else "Публикую…"))
            done = _publish_button(page)
            for _ in range(120):
                try:
                    if done.is_enabled():
                        break
                except Exception:  # noqa: BLE001
                    done = _publish_button(page)
                time.sleep(5)
            done.click()
            time.sleep(5)
            # If Studio still shows the upload going (it lets one publish before the end), the tab stays open
            # until it is over: closing it would cut the upload off.
            for _ in range(6 * 720):
                if not _uploading(page):
                    break
                time.sleep(5)
            if not link:
                try:
                    link = page.locator("a[href*='youtu.be/']").first.get_attribute("href", timeout=5_000) or ""
                except Exception:  # noqa: BLE001
                    link = ""
            if not link:
                try:
                    link = page.locator("ytcp-video-share-dialog a, a#share-url").first.get_attribute("href", timeout=15_000) or ""
                except PWTimeout:
                    link = ""
            log("Запланировано" if scheduled else "Опубликовано")
            if result is not None:
                result["video_id"] = video_id(link)
                result["channel_id"], result["channel_name"] = want, channel_name
                result["scheduled"] = scheduled
            if thumb_ok is False:
                thumb_ok = _thumb_on_edit_page(page, link, thumb, log)
            if result is not None:
                result["thumb_ok"] = thumb_ok
                result["verified"] = _verify(page, link, title, log)
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
