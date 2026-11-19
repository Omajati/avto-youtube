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
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from . import media
from .net import UserError

ROOT = Path(__file__).parent / "webmotion"
GEO = Path(__file__).parent / "geo"
HOST = "http://webmotion.local/"
TYPES = {".webp": "image/webp", ".jpeg": "image/jpeg", ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".json": "application/json", ".woff2": "font/woff2", ".jpg": "image/jpeg", ".png": "image/png"}
_install_lock = threading.Lock()
_installed = False


def _file_for(url: str) -> Path | None:
    rel = url[len(HOST):].split("?", 1)[0]
    base, rest = (GEO, rel[4:]) if rel.startswith("geo/") else (ROOT, rel)
    path = (base / rest).resolve()
    return path if path.is_file() and base.resolve() in path.parents else None


def _serve(route, extra: dict | None = None):
    """extra: {"photo/<name>": Path} — the scene's own picture, served only to this one render."""
    rel = route.request.url[len(HOST):].split("?", 1)[0]
    path = (extra or {}).get(rel) or _file_for(route.request.url)
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


STATUS = {"browser": "unknown", "error": "", "last_failure": ""}  # shown by «Проверить новый движок»


def prepare_in_background():
    """At start: make sure the browser for the inserts is there (downloads it once), so a build never waits for it."""
    def run():
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                STATUS["browser"] = "checking"
                b = _launch(pw, ARGS[-1], lambda s: STATUS.update(browser="downloading"))
                b.close()
            STATUS.update(browser="ready", error="")
        except Exception as e:  # noqa: BLE001
            STATUS.update(browser="failed", error=str(e).splitlines()[0][:300] if str(e) else type(e).__name__)
    threading.Thread(target=run, daemon=True, name="webmotion-prepare").start()


def selftest() -> dict:
    """Draws a 2-second test insert; {"ok", "seconds", "error"} — the owner's way to see that the engine works."""
    import tempfile
    import time as _t
    out = Path(tempfile.mkdtemp(prefix="webmotion-test-")) / "test.mp4"
    t = _t.time()
    try:
        render_kind({"type": "counter", "title": "Проверка", "value": 245, "caption": "новый движок работает",
                     "tone": "warm", "theme": "navy"}, out, 2.0, 1920, 1080, 30, "ru")
        ok = out.is_file() and out.stat().st_size > 1000
        STATUS.update(browser="ready", error="")
        return {"ok": ok, "seconds": round(_t.time() - t, 1), "error": "" if ok else "файл не получился"}
    except Exception as e:  # noqa: BLE001
        msg = str(e).splitlines()[0][:400] if str(e) else type(e).__name__
        STATUS.update(error=msg)
        return {"ok": False, "seconds": round(_t.time() - t, 1), "error": msg}
    finally:
        shutil.rmtree(out.parent, ignore_errors=True)


# Ways to start the browser, best first: the Mac's graphics card (Metal), then software drawing, which always works.
ARGS = ([["--use-angle=metal", "--ignore-gpu-blocklist"]] if sys.platform == "darwin" else []) + \
       [["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]]


def _launch(pw, args, log):
    exe = os.environ.get("AY_CHROMIUM") or None
    try:
        return pw.chromium.launch(args=args, executable_path=exe)
    except Exception as e:  # noqa: BLE001
        if "executable doesn't exist" not in str(e).lower() and "playwright install" not in str(e).lower():
            raise
    _install_browser(log)
    return pw.chromium.launch(args=args, executable_path=exe)


def render(template: str, spec: dict, out: Path, seconds: float, w: int = 1920, h: int = 1080, fps: int = 30,
           poster: Path | None = None, log=lambda s: None, photo: Path | None = None):
    """Shoots templates/<template>.html into an MP4 of exactly round(seconds * fps) frames.
    photo: a picture shown by the template (spec["photo"] then holds its address)."""
    from playwright.sync_api import sync_playwright

    n = max(2, round(seconds * fps))
    # A scene picture (poster) is asked with a tiny clip: the animation is then timed as a full one and only
    # sampled, so its last frame shows everything that appears.
    timing = max(n, 240) if poster else n
    data = {**spec, "w": w, "h": h, "frames": timing, "fps": fps}
    extra = {}
    if photo and Path(photo).is_file():
        key = "photo/scene" + Path(photo).suffix.lower()
        extra[key] = Path(photo)
        data["photo"] = "../" + key
    out = Path(out)
    with sync_playwright() as pw:
        last = None
        for args in ARGS:
            try:
                browser = _launch(pw, args, log)
            except UserError:
                raise  # the browser could not be downloaded: no other way helps
            except Exception as e:  # noqa: BLE001 - this way of starting failed (e.g. the graphics card): try the next
                last = UserError(f"Браузер не запустился ({args[0]}): {str(e).splitlines()[0][:200] if str(e) else type(e).__name__}")
                continue
            try:
                page = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=1)
                errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.route(HOST + "**", lambda route: _serve(route, extra))
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
    def t(k: str, n: int) -> str:  # the text of a field, at most n characters, never cut inside a word
        v = " ".join(str(spec.get(k) or "").split())
        if len(v) <= n:
            return v
        cut = v[:n + 1].rsplit(" ", 1)[0].rstrip(",;:—-– ")
        return (cut or v[:n]) + "…"

    kind = spec.get("type")
    base = {"tag": t("title", 50), "lang": lang, "tone": spec.get("tone") or "hot",
            "theme": spec.get("theme") if spec.get("theme") in ("navy", "graphite") else "navy"}
    if kind == "counter":
        v = float(spec.get("value") or 0)
        return {**base, "value": v, "decimals": _dec(v), "prefix": t("prefix", 2), "suffix": t("suffix", 2),
                "unit": t("unit", 10), "caption": t("caption", 90), "note": t("note", 60)}
    if kind == "thermo":
        steps = [{"value": float(x.get("value")), "caption": str(x.get("caption") or "")[:50], "prefix": str(x.get("prefix") or "")[:2],
                  "tone": x.get("tone") or "cold", "decimals": _dec(x.get("value"))}
                 for x in (spec.get("steps") or [])[:3] if isinstance(x, dict)]
        return {**base, "unit": t("unit", 6) or "°C", "steps": steps}
    if kind == "scale":
        items = [{"name": str(x.get("label") or "")[:30], "value": float(x.get("value"))}
                 for x in (spec.get("points") or [])[:5] if isinstance(x, dict)]
        ref = {"value": float(spec.get("ref_value") or 0), "label": t("ref_label", 30)} if t("ref_label", 30) else None
        return {**base, "unit": t("unit", 6), "items": items, "ref": ref,
                "hero": {"name": t("hero_name", 30), "value": float(spec.get("hero_value") or 0), "note": t("hero_note", 30)}}
    if kind == "layer":
        return {**base, "value": float(spec.get("value") or 1), "unit": t("unit", 4) or ("м" if lang == "ru" else "m"),
                "caption": t("caption", 40), "material": spec.get("material") or "ice",
                "layer_label": t("layer_label", 20), "below_label": t("below_label", 20)}
    if kind == "chart":
        pts = [{"label": str(x.get("label") or "")[:14], "value": float(x.get("value"))}
               for x in (spec.get("points") or [])[:12] if isinstance(x, dict)]
        return {**base, "points": pts, "style": "bar" if spec.get("chart_style") == "bar" or len(pts) <= 3 else "line",
                "unit": t("unit", 8), "prefix": t("prefix", 2)}
    if kind == "timeline":
        ev = [{"label": str(e.get("label") or "")[:16], "text": str(e.get("text") or "")[:60]}
              for e in (spec.get("events") or [])[:6] if isinstance(e, dict) and str(e.get("label") or "").strip()]
        try:
            cur = int(spec.get("current"))
        except (TypeError, ValueError):
            cur = len(ev) - 1
        return {**base, "events": ev, "current": max(0, min(len(ev) - 1, cur))}
    if kind == "compare":
        rows = [{"label": str(r.get("label") or "")[:30], "left": float(r.get("left") or 0), "right": float(r.get("right") or 0)}
                for r in (spec.get("rows") or [])[:4] if isinstance(r, dict)]
        return {**base, "left_name": t("left_name", 24), "right_name": t("right_name", 24), "rows": rows, "unit": t("unit", 8)}
    if kind == "quote":
        return {**base, "quote": t("quote", 300), "author": t("author", 80)}
    if kind == "list":
        return {**base, "lines": [str(x)[:80] for x in (spec.get("lines") or [])[:6] if str(x).strip()]}
    if kind == "photonum":
        v = float(spec.get("value") or 0)
        return {**base, "value": v, "decimals": _dec(v), "prefix": t("prefix", 2), "suffix": t("suffix", 2),
                "unit": t("unit", 10), "caption": t("caption", 90)}
    if kind == "place":
        return {**base, "name": t("title", 60), "sub": t("note", 40)}
    if kind == "document":
        return {**base, "tag": "", "org": t("org", 50), "headline": t("headline", 90), "highlight": t("highlight", 120),
                "label": t("when_label", 20) if t("when", 40) else "", "value": t("when", 40)}
    if kind == "calendar":
        return {**base, "total": int(float(spec.get("total") or 365)), "filled": int(float(spec.get("filled") or 0)),
                "suffix": t("suffix", 2), "caption": t("caption", 60), "legend": t("legend", 50),
                "season": spec.get("season") or "even", "tone": spec.get("tone") or "cold"}
    raise UserError(f"Неизвестный вид моушена: {kind}")


_GEO_CACHE: dict = {}


def _geo(name: str):
    if name not in _GEO_CACHE:
        _GEO_CACHE[name] = json.loads((GEO / name).read_text("utf-8"))
    return _GEO_CACHE[name]


def find_river(name: str):
    """Natural Earth rivers by their English name: «Lena», «the Lena River», «Yenisei» (a few spellings)."""
    rivers = _geo("rivers.json")
    n = " ".join(str(name or "").lower().replace("river", "").replace("the ", " ").split())
    alias = {"yenisei": "yenisey", "enisei": "yenisey", "amur": "amur", "dnieper": "dnepr", "danube": "danube"}
    n = alias.get(n, n)
    for k in rivers:
        if k.lower() == n or k.lower().replace(" ", "") == n.replace(" ", ""):
            return k, rivers[k]
    for k in rivers:
        if k.lower().startswith(n + " ") or k.lower().endswith(" " + n):
            return k, rivers[k]
    return None, None


def _chain(lines: list, start_far_from=None) -> tuple[list, list]:
    """Joins a river's pieces end to end into one line (the others — branches, deltas — come back apart).
    The line is turned to start at its end farther from start_far_from (lat, lon): it then «flows» to the hero."""
    rest = [list(l) for l in lines if len(l) >= 2]
    if not rest:
        return [], []
    rest.sort(key=len, reverse=True)
    chain = rest.pop(0)
    near = lambda a, b: abs(a[0] - b[0]) < 0.2 and abs(a[1] - b[1]) < 0.2
    grew = True
    while grew:
        grew = False
        for i, l in enumerate(rest):
            if near(chain[-1], l[0]):
                chain += l[1:]
            elif near(chain[-1], l[-1]):
                chain += l[::-1][1:]
            elif near(chain[0], l[-1]):
                chain = l[:-1] + chain
            elif near(chain[0], l[0]):
                chain = l[::-1][:-1] + chain
            else:
                continue
            rest.pop(i)
            grew = True
            break
    if start_far_from:
        la, lo = start_far_from
        d = lambda p: (p[1] - la) ** 2 + ((p[0] - lo) * math.cos(math.radians(la))) ** 2
        if d(chain[-1]) > d(chain[0]):
            chain = chain[::-1]
    return chain, rest


def map_spec(spec: dict, lang: str) -> dict | None:
    """The old «map» (countries, a route, pins) on the dark relief map; None when the places spread over half the
    world (then a globe tells it better)."""
    from . import motion
    route = [p for p in spec.get("route") or [] if isinstance(p, dict)][:6]
    pins = [p for p in spec.get("pins") or [] if isinstance(p, dict)][:4]
    stops = [p for p in route + pins if _num_ok(p.get("lat")) and _num_ok(p.get("lon"))]
    found = [c for c in (motion.find_country(n) for n in spec.get("countries") or []) if c][:4]
    pts = [(float(p["lat"]), float(p["lon"])) for p in stops]
    for c in found:
        pts += [(q[1], q[0]) for ring in motion._main_rings(c) for q in ring[::max(1, len(ring) // 30)]]
    if not pts or _view(pts)["spread"] > 38:
        return None
    base = satmap_spec({"title": spec.get("title"), "pins": [{"name": p.get("name"), "lat": p["lat"], "lon": p["lon"]}
                                                              for p in stops], "theme": spec.get("theme"),
                        "countries": [], "rivers": [], "region": ""}, lang, extra_pts=pts)
    base["fills"] = [[[q[0], q[1]] for q in ring] for c in found for ring in motion._main_rings(c)]
    base["route"] = [[float(p["lon"]), float(p["lat"])] for p in route if p in stops]
    for p in base["pins"]:
        p["hero"] = False
    return base


def _num_ok(v) -> bool:
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def satmap_spec(spec: dict, lang: str, extra_pts: list | None = None) -> dict:
    """A region on the dark relief map: the box (16:9 at that latitude), rivers by name, railways inside the box,
    the chosen countries' outlines, the towns."""
    from . import motion
    pts, borders = [], []
    found = [c for c in (motion.find_country(n) for n in spec.get("countries") or []) if c][:3]
    for c in found:
        for ring in motion._main_rings(c):
            borders.append([[p[0], p[1]] for p in ring])
            pts += [(p[1], p[0]) for p in ring[::max(1, len(ring) // 40)]]
    rivers = []
    for r in (spec.get("rivers") or [])[:3]:
        key = r.get("name") if isinstance(r, dict) else r
        label = (r.get("label") if isinstance(r, dict) else "") or ""
        k, lines = find_river(key)
        if lines:
            rivers.append({"name": k, "label": str(label)[:20], "raw": lines})
            pts += [(p[1], p[0]) for ln in lines for p in ln[::4]]
    pins = []
    for i, p in enumerate((spec.get("pins") or [])[:5]):
        try:
            lat, lon = float(p.get("lat")), float(p.get("lon"))
        except (TypeError, ValueError, AttributeError):
            continue
        pins.append({"name": str(p.get("name") or "")[:30], "lat": lat, "lon": lon, "hero": i == 0})
        pts.append((lat, lon))
    pts += list(extra_pts or [])
    if not pts:
        raise UserError("Для карты нет ни реки, ни страны, ни точки.")
    view = _view(pts)
    lat0, lon0 = view["lat"], view["lon"]
    lons = [((lo - lon0 + 540) % 360) - 180 for _, lo in pts]
    lats = [la for la, _ in pts]
    w_deg = (max(lons) - min(lons)) * 1.18 + 2
    h_deg = (max(lats) - min(lats)) * 1.25 + 2
    cosl = max(0.2, math.cos(math.radians(lat0)))
    if w_deg * cosl / h_deg < 16 / 9:  # keep 16:9 on screen at this latitude
        w_deg = h_deg * 16 / 9 / cosl
    else:
        h_deg = w_deg * cosl * 9 / 16
    cx = lon0 + (max(lons) + min(lons)) / 2
    cy = min(84 - h_deg / 2, max(-84 + h_deg / 2, (max(lats) + min(lats)) / 2))
    box = {"w": cx - w_deg / 2, "e": cx + w_deg / 2, "s": cy - h_deg / 2, "n": cy + h_deg / 2}
    hero = (pins[0]["lat"], pins[0]["lon"]) if pins else (cy, cx)
    for r in rivers:  # one flowing line (towards the hero), the branches apart
        main, rest = _chain(r.pop("raw"), hero)
        r["lines"] = [main] + rest
    rails = []
    if spec.get("show_rail"):
        # only the railways around the hero (within about a third of the map): «the line ends before the river»
        cosh = max(0.2, math.cos(math.radians(hero[0])))
        reach = 0.34 * w_deg * cosh
        dist = lambda p: math.hypot((p[0] - hero[1]) * cosh, p[1] - hero[0])
        near = [(min(dist(p) for p in ln), ln) for ln in _geo("railroads.json")
                if box["w"] <= max(p[0] for p in ln) and min(p[0] for p in ln) <= box["e"]
                and box["s"] <= max(p[1] for p in ln) and min(p[1] for p in ln) <= box["n"]]
        rails = [ln for d, ln in sorted(near, key=lambda x: x[0]) if d <= reach][:80]
    region = None
    name = str(spec.get("region") or "").strip()
    if name:
        if found:
            region = {"name": name[:30], "lat": found[0]["ly"], "lon": found[0]["lx"]}
        else:
            region = {"name": name[:30], "lat": cy + h_deg * 0.12, "lon": cx - w_deg * 0.15}
    t = str(spec.get("title") or "").strip()[:60]
    return {"tag": t, "box": box, "region": region, "rivers": rivers, "rails": rails[:4000], "borders": borders,
            "pins": pins, "theme": spec.get("theme") if spec.get("theme") in ("navy", "graphite") else "navy"}


def render_kind(spec: dict, out: Path, seconds: float, w: int, h: int, fps: int, lang: str = "en",
                poster: Path | None = None, log=lambda s: None, photo: Path | None = None):
    """Any insert of the new engine by its motion type; every failure becomes a UserError (the scene keeps its picture)."""
    kind = spec.get("type")
    try:
        if kind == "satmap":
            render("satmap", satmap_spec(spec, lang), out, seconds, w, h, fps, poster, log)
        elif kind == "map":
            m = map_spec(spec, lang)
            if m:
                render("satmap", m, out, seconds, w, h, fps, poster, log)
            else:  # spread over half the world: the globe, in the channel's style
                render("globe3d" if str(spec.get("style") or "3d") != "2d" else "globe2d", globe_spec(spec, lang),
                       out, seconds, w, h, fps, poster, log)
        elif kind == "globe":
            style = "globe2d" if str(spec.get("style") or "3d").lower() in ("2d", "globe2d", "flat") else "globe3d"
            render(style, globe_spec(spec, lang), out, seconds, w, h, fps, poster, log)
        else:
            template = "calendar" if kind == "calendar" else kind
            render(template, card_spec(spec, lang), out, seconds, w, h, fps, poster, log,
                   photo=photo if kind in ("photonum", "place") else None)
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
