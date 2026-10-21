"""Starts the app. With --window (how «Авто ютуб.app» starts it) it opens in its own Mac window, otherwise in the browser."""
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

PORT = int(os.environ.get("AY_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}"
HERE = Path(__file__).resolve().parent

RU_MENU = {
    "global.quitConfirmation": "Ролик ещё делается. Закрыть программу? Работа продолжится с того же места при следующем запуске.",
    "global.quit": "Закрыть", "global.cancel": "Отмена", "global.ok": "OK", "global.saveFile": "Сохранить файл",
    "cocoa.menu.about": "О программе", "cocoa.menu.services": "Службы", "cocoa.menu.view": "Вид",
    "cocoa.menu.edit": "Правка", "cocoa.menu.hide": "Скрыть", "cocoa.menu.hideOthers": "Скрыть остальные",
    "cocoa.menu.showAll": "Показать все", "cocoa.menu.quit": "Завершить", "cocoa.menu.fullscreen": "Во весь экран",
    "cocoa.menu.cut": "Вырезать", "cocoa.menu.copy": "Скопировать", "cocoa.menu.paste": "Вставить",
    "cocoa.menu.selectAll": "Выбрать все",
}


def port_busy() -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def serve():
    import uvicorn
    uvicorn.run("app.server:app", host="127.0.0.1", port=PORT, log_level="warning")


def wait_for_server():
    for _ in range(120):
        if port_busy():
            return
        time.sleep(0.25)


def jobs_running() -> bool:
    try:
        from app import db
        return any(j.get("status") in ("running", "queued") for j in db.list_all())
    except Exception:
        return False


def dark_mac() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import subprocess
        return "Dark" in subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True,
                                        text=True, timeout=3).stdout
    except Exception:
        return False


def run_window():
    """Own window through the system WebKit. Falls back to the browser when that is not possible."""
    try:
        import webview
    except Exception as e:
        print(f"Окно программы недоступно ({e}), открываю в браузере.")
        return run_browser()
    if sys.platform == "darwin":
        try:  # the menu says «Авто ютуб», not «Python»
            from Foundation import NSBundle
            info = NSBundle.mainBundle().localizedInfoDictionary() or NSBundle.mainBundle().infoDictionary()
            info["CFBundleName"] = "Авто ютуб"
        except Exception:
            pass
    own_server = not port_busy()
    if own_server:
        threading.Thread(target=serve, daemon=True).start()
    wait_for_server()

    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    win = webview.create_window("Авто ютуб", URL, width=1360, height=880, min_size=(900, 600),
                                background_color="#0f1115" if dark_mac() else "#f5f5f7", text_select=True)

    def closing(window):
        # Ask only while a video is being made: closing pauses it until the next start.
        window.confirm_close = jobs_running()
        return True

    win.events.closing += closing

    def watch_server():
        # «Выключить программу» stops the server: the window closes too.
        while port_busy():
            time.sleep(1)
        try:
            win.destroy()
        except Exception:
            pass

    def started():
        threading.Thread(target=watch_server, daemon=True).start()
        if sys.platform == "darwin":
            try:
                from AppKit import NSApplication, NSImage
                icon = HERE / "app" / "static" / "icon-1024.png"
                NSApplication.sharedApplication().setApplicationIconImage_(
                    NSImage.alloc().initWithContentsOfFile_(str(icon)))
            except Exception:
                pass

    data = Path(os.environ.get("AY_DATA", HERE / "data"))
    (data / "window").mkdir(parents=True, exist_ok=True)
    try:
        webview.start(started, private_mode=False, storage_path=str(data / "window"), localization=RU_MENU)
    except Exception as e:
        print(f"Окно программы не открылось ({e}), открываю в браузере.")
        webbrowser.open(URL)
        while own_server and port_busy():
            time.sleep(5)
    os._exit(0)  # the window is closed: the program stops


def run_browser():
    if port_busy():
        print("Программа уже запущена, открываю окно…")
        webbrowser.open(URL)
        return

    def open_later():
        wait_for_server()
        webbrowser.open(URL)

    threading.Thread(target=open_later, daemon=True).start()
    print(f"Авто ютуб работает: {URL}")
    serve()


if __name__ == "__main__":
    os.chdir(HERE)
    sys.path.insert(0, str(HERE))
    if "--window" in sys.argv:
        run_window()
    elif "--no-browser" in sys.argv:
        if not port_busy():
            print(f"Авто ютуб работает: {URL}")
            serve()
    else:
        run_browser()
