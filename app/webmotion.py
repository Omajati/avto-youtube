"""Motion design drawn as web pages: each insert is an HTML template (three.js, d3, CSS) in app/webmotion/templates,
filled with the scene's data and shot frame by frame in a headless Chromium, then encoded like any other clip.

Every template exposes window.renderFrame(f) and sets window.READY when loaded; the page is never played in real
time, so the motion is exactly smooth whatever the speed of the machine. Files are served from app/webmotion (and
the country shapes from app/geo) on a made-up address the browser never leaves.
The browser is Playwright's own Chromium: if it is missing it is downloaded once (about 150 MB)."""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
from pathlib import Path

from . import media
from .net import UserError

ROOT = Path(__file__).parent / "webmotion"
GEO = Path(__file__).parent / "geo"
HOST = "http://webmotion.local/"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".json": "application/json", ".woff2": "font/woff2", ".jpg": "image/jpeg", ".png": "image/png"}
_install_lock = threading.Lock()
_installed = False


def _file_for(url: str) -> Path | None:
    rel = url[len(HOST):].split("?", 1)[0]
    base, rest = (GEO, rel[4:]) if rel.startswith("geo/") else (ROOT, rel)
    path = (base / rest).resolve()
    return path if path.is_file() and base.resolve() in path.parents else None


def _serve(route):
    path = _file_for(route.request.url)
    if not path:
        route.fulfill(status=404, body="")
        return
    route.fulfill(status=200, body=path.read_bytes(), content_type=TYPES.get(path.suffix.lower(), "application/octet-stream"))


def _install_browser(log):
    global _installed
    with _install_lock:
        if _installed:
            return
        log("Скачиваю браузер для моушен-графики (один раз, около 150 МБ)…")
        r = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], capture_output=True, text=True,
                           timeout=1800)
        if r.returncode != 0:
            raise UserError("Не получилось скачать браузер для моушен-графики: " + (r.stderr or r.stdout)[-300:])
        _installed = True


# Ways to start the browser, best first: the Mac's graphics card (Metal), then software drawing, which always works.
ARGS = ([["--use-angle=metal", "--ignore-gpu-blocklist"]] if sys.platform == "darwin" else []) + \
       [["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]]


def _launch(pw, args, log):
    exe = os.environ.get("AY_CHROMIUM") or None
    try:
        return pw.chromium.launch(args=args, executable_path=exe)
    except Exception as e:  # noqa: BLE001
        if "Executable doesn't exist" not in str(e) and "playwright install" not in str(e):
            raise
    _install_browser(log)
    return pw.chromium.launch(args=args, executable_path=exe)


def render(template: str, spec: dict, out: Path, seconds: float, w: int = 1920, h: int = 1080, fps: int = 30,
           poster: Path | None = None, log=lambda s: None):
    """Shoots templates/<template>.html into an MP4 of exactly round(seconds * fps) frames."""
    from playwright.sync_api import sync_playwright

    n = max(2, round(seconds * fps))
    # A scene picture (poster) is asked with a tiny clip: the animation is then timed as a full one and only
    # sampled, so its last frame shows everything that appears.
    timing = max(n, 240) if poster else n
    data = {**spec, "w": w, "h": h, "frames": timing, "fps": fps}
    out = Path(out)
    with sync_playwright() as pw:
        last = None
        for args in ARGS:
            browser = _launch(pw, args, log)
            try:
                page = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=1)
                errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.route(HOST + "**", _serve)
                page.add_init_script("window.SPEC = " + json.dumps(data, ensure_ascii=False) + ";")
                page.goto(HOST + f"templates/{template}.html", wait_until="load", timeout=60_000)
                try:
                    page.wait_for_function("window.READY === true", timeout=120_000)
                except Exception:  # noqa: BLE001
                    last = UserError(f"Шаблон «{template}» не загрузился: {'; '.join(errors)[:300] or 'нет ответа'}")
                    continue  # e.g. no WebGL with the graphics card: the next way of drawing
                tmp = out.with_suffix(".web.tmp.mp4")
                ff = subprocess.Popen([media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "image2pipe",
                                       "-c:v", "mjpeg", "-framerate", str(fps), "-i", "-", "-frames:v", str(n),
                                       "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                                       "-r", str(fps), "-movflags", "+faststart", str(tmp)],
                                      stdin=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    for i in range(n):
                        f = i if timing == n else round(i * (timing - 1) / (n - 1))
                        page.evaluate("f => window.renderFrame(f)", f)
                        ff.stdin.write(page.screenshot(type="jpeg", quality=94))
                    if poster:
                        page.screenshot(path=str(poster), type="png")
                except BrokenPipeError:
                    pass
                finally:
                    try:
                        ff.stdin.close()
                    except OSError:
                        pass
                    err = ff.stderr.read().decode("utf-8", "replace") if ff.stderr else ""
                    ff.wait()
                if ff.returncode != 0 or not tmp.is_file():
                    raise UserError(f"Не получилось склеить моушен-вставку: {err[-300:]}")
                if errors:
                    tmp.unlink(missing_ok=True)
                    raise UserError(f"Ошибка в шаблоне «{template}»: {errors[0][:300]}")
                tmp.replace(out)
                return
            finally:
                browser.close()
        raise last or UserError("Не получилось нарисовать моушен-вставку.")


# ---------------------------------------------------------------- the globe

def _xyz(lat: float, lon: float):
    la, lo = math.radians(lat), math.radians(lon)
    return math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)


def _view(points: list[tuple[float, float]]) -> dict:
    """Where the camera looks (the spherical middle of all points) and how far the points spread from it."""
    if not points:
        return {"lat": 20.0, "lon": 0.0, "spread": 60.0}
    vs = [_xyz(la, lo) for la, lo in points]
    x, y, z = (sum(v[i] for v in vs) / len(vs) for i in range(3))
    r = math.sqrt(x * x + y * y + z * z) or 1.0
    lat, lon = math.degrees(math.asin(max(-1.0, min(1.0, z / r)))), math.degrees(math.atan2(y, x))
    c = _xyz(lat, lon)
    spread = max(math.degrees(math.acos(max(-1.0, min(1.0, sum(a * b for a, b in zip(c, v)))))) for v in vs)
    return {"lat": round(lat, 3), "lon": round(lon, 3), "spread": round(max(spread, 6.0), 2)}


def globe_spec(spec: dict, lang: str = "en") -> dict:
    """The data a globe template needs, from a motion spec: countries as their codes and a point each, the places,
    the plate and the view."""
    from . import motion

    def place(p):
        try:
            lat, lon = float(p.get("lat")), float(p.get("lon"))
        except (TypeError, ValueError):
            return None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        return {"name": str(p.get("name") or "").strip()[:40], "note": str(p.get("note") or "").strip()[:60],
                "lat": lat, "lon": lon}

    route = [x for x in (place(p) for p in spec.get("route") or [] if isinstance(p, dict)) if x][:6]
    pins = [x for x in (place(p) for p in spec.get("pins") or [] if isinstance(p, dict)) if x][:5]
    found = [c for c in (motion.find_country(n) for n in spec.get("countries") or []) if c][:8]
    countries, cpoints, view_pts = [], [], []
    for c in found:
        if c["a3"] in countries:
            continue
        countries.append(c["a3"])
        cpoints.append({"name": c.get(lang) or c.get("en") or c["a3"], "note": "", "lat": c["ly"], "lon": c["lx"]})
        for ring in motion._main_rings(c):
            view_pts += [(p[1], p[0]) for p in ring[::max(1, len(ring) // 24)]]
    view_pts += [(p["lat"], p["lon"]) for p in route + pins] or [(p["lat"], p["lon"]) for p in cpoints]
    return {"countries": countries, "country_points": cpoints, "route": route, "pins": pins,
            "tag": str(spec.get("title") or "").strip()[:60], "headline": str(spec.get("headline") or "").strip()[:60],
            "subline": str(spec.get("subline") or "").strip()[:90], "view": _view(view_pts)}


def render_globe(spec: dict, out: Path, seconds: float, w: int, h: int, fps: int, lang: str = "en",
                 poster: Path | None = None, log=lambda s: None):
    style = "globe2d" if str(spec.get("style") or "3d").lower() in ("2d", "globe2d", "flat") else "globe3d"
    try:
        render(style, globe_spec(spec, lang), out, seconds, w, h, fps, poster, log)
    except UserError:
        raise
    except Exception as e:  # noqa: BLE001 - the browser's own errors: the scene then keeps its picture
        raise UserError(f"Глобус не нарисовался: {str(e).splitlines()[0][:200] if str(e) else type(e).__name__}")


def _dec(v) -> int:
    v = float(v)
    if v.is_integer():
        return 0
    return min(2, len(repr(abs(v)).split(".")[1]))


def card_spec(spec: dict, lang: str) -> dict:
    """The data of a «card» template (counter, thermo, calendar) from a motion spec."""
    kind, t = spec.get("type"), lambda k, n: str(spec.get(k) or "").strip()[:n]
    base = {"tag": t("title", 50), "lang": lang, "tone": spec.get("tone") or "hot"}
    if kind == "counter":
        v = float(spec.get("value") or 0)
        return {**base, "value": v, "decimals": _dec(v), "prefix": t("prefix", 2), "suffix": t("suffix", 2),
                "unit": t("unit", 10), "caption": t("caption", 60), "note": t("note", 50)}
    if kind == "thermo":
        steps = [{"value": float(x.get("value")), "caption": str(x.get("caption") or "")[:50], "prefix": str(x.get("prefix") or "")[:2],
                  "tone": x.get("tone") or "cold", "decimals": _dec(x.get("value"))}
                 for x in (spec.get("steps") or [])[:3] if isinstance(x, dict)]
        return {**base, "unit": t("unit", 6) or "°C", "steps": steps}
    if kind == "calendar":
        return {**base, "total": int(float(spec.get("total") or 365)), "filled": int(float(spec.get("filled") or 0)),
                "suffix": t("suffix", 2), "caption": t("caption", 60), "legend": t("legend", 50),
                "season": spec.get("season") or "even", "tone": spec.get("tone") or "cold"}
    raise UserError(f"Неизвестный вид моушена: {kind}")


def render_kind(spec: dict, out: Path, seconds: float, w: int, h: int, fps: int, lang: str = "en",
                poster: Path | None = None, log=lambda s: None):
    """Any insert of the new engine by its motion type; every failure becomes a UserError (the scene keeps its picture)."""
    kind = spec.get("type")
    try:
        if kind == "globe":
            style = "globe2d" if str(spec.get("style") or "3d").lower() in ("2d", "globe2d", "flat") else "globe3d"
            render(style, globe_spec(spec, lang), out, seconds, w, h, fps, poster, log)
        else:
            render(kind, card_spec(spec, lang), out, seconds, w, h, fps, poster, log)
    except UserError:
        raise
    except Exception as e:  # noqa: BLE001 - the browser's own errors
        raise UserError(f"Моушен «{kind}» не нарисовался: {str(e).splitlines()[0][:200] if str(e) else type(e).__name__}")


def globe_ok(spec: dict) -> bool:
    from . import motion
    pts = [p for p in (spec.get("route") or []) + (spec.get("pins") or []) if isinstance(p, dict)]
    good = 0
    for p in pts:
        try:
            if -90 <= float(p.get("lat")) <= 90 and -180 <= float(p.get("lon")) <= 180:
                good += 1
        except (TypeError, ValueError):
            pass
    return good >= 1 or any(motion.find_country(n) for n in spec.get("countries") or [])
