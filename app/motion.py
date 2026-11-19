"""Motion design inserts in place of a picture: what they are (KINDS), how the model plans them (PLAN_PROMPT, SCHEMA,
clean_plan), the checks that a plan can be drawn, the sounds that go with them (cues) and the country shapes.
Everything is drawn by the new engine (webmotion.py): the old flat drawings were removed (the owner, 19.11.2026)."""
import json
import math
import re
from functools import lru_cache
from pathlib import Path

from .media import UserError


GEO = Path(__file__).parent / "geo" / "countries.json"


KINDS = {"map": "🗺 Карта", "timeline": "📅 Линия времени", "chart": "📈 График", "compare": "⚖️ Сравнение",
         "quote": "💬 Цитата", "list": "📋 Список", "globe": "🌍 Глобус", "counter": "🔢 Цифра",
         "thermo": "🌡 Термометр", "calendar": "🟦 Сетка-календарь", "photonum": "🖼 Цифра поверх фото",
         "place": "📍 Плашка места", "document": "📄 Документ", "scale": "📏 Шкала", "layer": "🧍 Слой и человек",
         "satmap": "🛰 Карта рельефа"}
FAILURES: list = []  # (kind, reason) each time an insert could not be drawn (the build shows it on the project card)


WEB_KINDS = ("globe", "counter", "thermo", "calendar", "photonum", "place", "document", "scale", "layer", "chart",
             "satmap", "map", "timeline", "compare", "quote", "list")  # every kind: all drawn by the new engine


MODES = {"auto": "Авто: карты и даты там, где они к месту", "off": "Выключено"}


@lru_cache(maxsize=1)
def countries() -> list:
    return json.loads(GEO.read_text("utf-8"))


def _norm(s: str) -> str:
    return re.sub(r"[^\w]", "", (s or "").lower().replace("ё", "е"))


ALIASES = {"usa": "USA", "сша": "USA", "america": "USA", "unitedstates": "USA", "uk": "GBR", "britain": "GBR",
           "великобритания": "GBR", "англия": "GBR", "england": "GBR", "россия": "RUS", "russia": "RUS",
           "ссср": "RUS", "ussr": "RUS", "китай": "CHN", "china": "CHN", "абиссиния": "ETH", "abyssinia": "ETH",
           "персия": "IRN", "persia": "IRN", "бирма": "MMR", "burma": "MMR", "занзибар": "TZA", "zanzibar": "TZA",
           "конго": "COD", "congo": "COD", "корея": "KOR", "korea": "KOR", "чехия": "CZE", "czechia": "CZE"}


def find_country(name: str):
    n = _norm(name)
    if not n:
        return None
    code = ALIASES.get(n)
    for c in countries():
        if code and c["a3"] == code:
            return c
        if n.upper() in (c["a3"], c["iso"]) or n in {_norm(c.get(k) or "") for k in ("en", "ru", "de", "es", "fr")}:
            return c
    return None


def _ring_area(r: list) -> float:
    return abs(sum(r[i][0] * r[i - 1][1] - r[i - 1][0] * r[i][1] for i in range(len(r)))) / 2


def _main_rings(c: dict) -> list:
    """The rings of a country near its biggest one: far overseas islands do not stretch the view."""
    big = max(c["r"], key=_ring_area)
    bx = sum(p[0] for p in big) / len(big)
    by = sum(p[1] for p in big) / len(big)
    out = []
    for r in c["r"]:
        mx, my = sum(p[0] for p in r) / len(r), sum(p[1] for p in r) / len(r)
        if abs(mx - bx) < 25 and abs(my - by) < 20:
            out.append(r)
    return out or [big]


# ---------------------------------------------------------------- map


def _points(spec: dict, key: str) -> list:
    out = []
    for p in spec.get(key) or []:
        try:
            lat, lon = float(p.get("lat")), float(p.get("lon"))
        except (TypeError, ValueError, AttributeError):
            continue
        if -85 <= lat <= 85 and -180 <= lon <= 180:
            out.append({"name": str(p.get("name") or "").strip()[:28], "lat": lat, "lon": lon})
    return out


def map_ok(spec: dict) -> bool:
    return bool([c for c in spec.get("countries") or [] if find_country(str(c))]) or len(_points(spec, "route")) >= 2 \
        or len(_points(spec, "pins")) >= 1


def timeline_ok(spec: dict) -> bool:
    ev = [e for e in spec.get("events") or [] if isinstance(e, dict) and str(e.get("label") or "").strip()]
    return 2 <= len(ev) <= 7


def _chart_points(spec):
    out = []
    for p in spec.get("points") or []:
        if isinstance(p, dict) and _num(p.get("value")) is not None and str(p.get("label") or "").strip():
            out.append((str(p["label"]).strip()[:12], _num(p["value"])))
    return out[:10]


def chart_ok(spec: dict) -> bool:
    return len(_chart_points(spec)) >= 2


def _rows(spec):
    out = []
    for r in spec.get("rows") or []:
        if isinstance(r, dict) and str(r.get("label") or "").strip():
            a, b = _num(r.get("left")), _num(r.get("right"))
            if a is not None and b is not None and (a > 0 or b > 0):
                out.append((str(r["label"]).strip()[:24], max(0.0, a), max(0.0, b)))
    return out[:5]


def compare_ok(spec: dict) -> bool:
    return len(_rows(spec)) >= 1 and bool(str(spec.get("left_name") or "").strip()) \
        and bool(str(spec.get("right_name") or "").strip())


def quote_ok(spec: dict) -> bool:
    q = str(spec.get("quote") or "").strip()
    return 3 <= len(q.split()) <= 40


def _lines(spec):
    return [str(x).strip()[:60] for x in spec.get("lines") or [] if str(x).strip()][:6]


def list_ok(spec: dict) -> bool:
    return 2 <= len(_lines(spec)) <= 6


def render(spec: dict, out: Path, seconds: float, w: int, h: int, fps: int, lang: str = "ru", poster: Path = None,
           prev: Path = None, photo: Path = None):
    """Draws an insert with the new engine (webmotion.py). If it cannot, the error goes up: the scene keeps its picture
    and the project card says why — there is no old drawing to fall back to (the owner's decision, 19.11.2026)."""
    kind = spec.get("type")
    if kind not in WEB_KINDS:
        raise UserError(f"Неизвестный вид моушена: {kind}")
    from . import webmotion
    try:
        webmotion.render_kind(spec, out, seconds, w, h, fps, lang, poster, photo=photo)
    except UserError as e:
        FAILURES.append((kind, str(e)))
        webmotion.STATUS["last_failure"] = str(e)[:300]
        raise


def cues(spec: dict, seconds: float) -> list:
    """Moments (seconds from the scene start) for soft sounds that match what appears: pops for points
    and pins, ticks for list items and rows. Kept to the renderers' own timing."""
    kind, out = spec.get("type"), []
    if kind == "map":
        # the relief map: fills ~0.1, the route draws 0.3–0.78, pins at 0.33
        route = _points(spec, "route")
        if len(route) > 1:
            out += [(0.3 + 0.48 * k / (len(route) - 1), "pop") for k in range(len(route))]
        out += [(0.34 + 0.05 * k, "pop") for k in range(len(_points(spec, "pins")))]
    elif kind == "timeline":
        ev = [e for e in spec.get("events") or [] if isinstance(e, dict) and str(e.get("label") or "").strip()][:6]
        m = len(ev)
        out += [(0.12 + 0.6 * (k / (m - 1) if m > 1 else 0) + 0.02, "pop") for k in range(m)]
    elif kind == "chart":
        m = len(_chart_points(spec))
        bars = spec.get("chart_style") == "bar" or m <= 3
        # the new chart (webmotion): bars land one by one; a line passes its points, then the bracket comes
        out += [((0.32 + 0.06 * k) if bars else (0.12 + 0.5 * k / max(1, m - 1)), "pop") for k in range(m)]
        if not bars and m > 1:
            out.append((0.68, "tick"))
    elif kind == "compare":
        out += [(0.14 + 0.12 * k + 0.2, "tick") for k in range(len(_rows(spec)))]
    elif kind == "list":
        m = len(_lines(spec))
        out += [(0.1 + 0.7 * k / max(m, 1) + 0.02, "tick") for k in range(m)]
    elif kind == "globe":
        # the same timing as globe_common.js: the first stop at 35 %, the others as the route reaches them, pins after
        route = [p for p in spec.get("route") or [] if isinstance(p, dict)][:6]
        pins = [p for p in spec.get("pins") or [] if isinstance(p, dict)][:5]
        if route:
            out.append((0.35, "pop"))
            out += [(0.40 + 0.34 * k / (len(route) - 1) - 0.02, "pop") for k in range(1, len(route))]
        out += [(min(0.36 + 0.07 * k, 0.72), "pop") for k in range(len(pins))]
    elif kind == "counter":
        out.append((0.46, "pop"))  # the number lands
    elif kind == "thermo":
        steps = [x for x in spec.get("steps") or [] if isinstance(x, dict)][:3]
        out += [(0.08 + k * 0.9 / max(1, len(steps)) + 0.36 / max(1, len(steps)), "pop") for k in range(len(steps))]
    elif kind == "calendar":
        out.append((0.82, "pop"))
    elif kind == "photonum":
        out.append((0.3, "pop"))
    elif kind == "scale":
        n = len([p for p in spec.get("points") or [] if isinstance(p, dict)][:6])
        out += [(0.26 + k * 0.34 / max(n, 1) + 0.02, "tick") for k in range(n)] + [(0.68, "pop")]
    elif kind == "layer":
        out.append((0.45, "pop"))
    elif kind == "satmap":
        out += [(0.33, "pop")] + ([(0.53, "pop")] if spec.get("pins") else [])
    elif kind == "place":
        out.append((0.12, "tick"))
    elif kind == "document":
        out += [(0.1, "tick")] + ([(0.42, "tick")] if str(spec.get("highlight") or "").strip() else [])
    elif kind == "quote" and str(spec.get("author") or "").strip():
        out.append((0.71, "tick"))
    # Never two sounds closer than a fifth of a second, and none in the first half second (the whoosh).
    res = []
    for f, name in sorted(out):
        at = f * seconds
        if at >= 0.5 and at < seconds - 0.1 and (not res or at - res[-1][0] >= 0.2):
            res.append((round(at, 3), name))
    return res[:12]


def globe_ok(spec: dict) -> bool:
    from . import webmotion
    return webmotion.globe_ok(spec)


def _num(v):
    try:
        return float(str(v).replace(" ", "").replace(",", ".").replace("−", "-"))
    except (TypeError, ValueError):
        return None


def counter_ok(spec: dict) -> bool:
    return _num(spec.get("value")) is not None and bool(str(spec.get("caption") or "").strip())


def thermo_ok(spec: dict) -> bool:
    steps = [x for x in spec.get("steps") or [] if isinstance(x, dict)]
    return 1 <= len(steps) <= 3 and all(_num(x.get("value")) is not None for x in steps)


def calendar_ok(spec: dict) -> bool:
    total, filled = _num(spec.get("total")), _num(spec.get("filled"))
    return total is not None and filled is not None and 2 <= total <= 1000 and 0 < filled <= total


def photonum_ok(spec: dict) -> bool:
    return counter_ok(spec)


def place_ok(spec: dict) -> bool:
    return bool(str(spec.get("title") or "").strip())


def document_ok(spec: dict) -> bool:
    return bool(str(spec.get("headline") or "").strip())


def scale_ok(spec: dict) -> bool:
    pts = [p for p in spec.get("points") or [] if isinstance(p, dict) and _num(p.get("value")) is not None]
    return len(pts) >= 2 and bool(str(spec.get("hero_name") or "").strip()) and _num(spec.get("hero_value")) is not None


def layer_ok(spec: dict) -> bool:
    v = _num(spec.get("value"))
    return v is not None and 0 < v <= 2000 and bool(str(spec.get("caption") or "").strip())


def satmap_ok(spec: dict) -> bool:
    from . import webmotion
    rivers = [r for r in spec.get("rivers") or [] if webmotion.find_river(r.get("name") if isinstance(r, dict) else r)[1]]
    pins = [p for p in spec.get("pins") or [] if isinstance(p, dict) and _num(p.get("lat")) is not None]
    return bool(rivers or pins or any(find_country(n) for n in spec.get("countries") or []))


CHECK = {"satmap": satmap_ok, "scale": scale_ok, "layer": layer_ok, "photonum": photonum_ok, "place": place_ok, "document": document_ok, "map": map_ok, "timeline": timeline_ok, "chart": chart_ok, "compare": compare_ok, "quote": quote_ok,
         "list": list_ok, "globe": globe_ok, "counter": counter_ok, "thermo": thermo_ok, "calendar": calendar_ok}


def poster(spec: dict, path: Path, w: int, h: int, lang: str = "ru"):
    """The last frame as the scene's picture (shown in the scene list; a short clip is drawn and thrown away)."""
    tmp = path.with_suffix(".poster.mp4")
    try:
        render(spec, tmp, 0.4, w, h, 10, lang, poster=path)
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------- the plan


FIELDS = ("index", "type", "title", "countries", "route", "pins", "events", "current", "points", "chart_style", "unit",
          "left_name", "right_name", "rows", "quote", "author", "lines",
          "value", "prefix", "suffix", "caption", "note", "tone", "steps", "total", "filled", "legend", "season",
          "org", "headline", "highlight", "when_label", "when",
          "hero_name", "hero_value", "hero_note", "ref_value", "ref_label", "material", "layer_label", "below_label",
          "rivers", "show_rail", "region")


_PLACE = {"type": "object", "properties": {"name": {"type": "string"}, "lat": {"type": "number"},
                                           "lon": {"type": "number"}}, "required": ["name", "lat", "lon"]}


SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
    "index": {"type": "integer"}, "type": {"type": "string", "enum": list(KINDS)},
    "title": {"type": "string"},
    "countries": {"type": "array", "items": {"type": "string"}},
    "route": {"type": "array", "items": _PLACE},
    "pins": {"type": "array", "items": _PLACE},
    "events": {"type": "array", "items": {"type": "object", "properties": {
        "label": {"type": "string"}, "text": {"type": "string"}}, "required": ["label", "text"]}},
    "current": {"type": "integer"},
    "points": {"type": "array", "items": {"type": "object", "properties": {
        "label": {"type": "string"}, "value": {"type": "number"}}, "required": ["label", "value"]}},
    "chart_style": {"type": "string", "enum": ["line", "bar"]},
    "unit": {"type": "string"},
    "left_name": {"type": "string"}, "right_name": {"type": "string"},
    "rows": {"type": "array", "items": {"type": "object", "properties": {
        "label": {"type": "string"}, "left": {"type": "number"}, "right": {"type": "number"}},
        "required": ["label", "left", "right"]}},
    "quote": {"type": "string"}, "author": {"type": "string"},
    "lines": {"type": "array", "items": {"type": "string"}},
    "value": {"type": "number"}, "prefix": {"type": "string"}, "suffix": {"type": "string"},
    "caption": {"type": "string"}, "note": {"type": "string"},
    "tone": {"type": "string", "enum": ["cold", "hot", "warm"]},
    "steps": {"type": "array", "items": {"type": "object", "properties": {
        "value": {"type": "number"}, "caption": {"type": "string"}, "prefix": {"type": "string"},
        "tone": {"type": "string", "enum": ["cold", "hot", "warm"]}}, "required": ["value", "caption", "prefix", "tone"]}},
    "total": {"type": "number"}, "filled": {"type": "number"}, "legend": {"type": "string"},
    "season": {"type": "string", "enum": ["winter", "summer", "even"]},
    "org": {"type": "string"}, "headline": {"type": "string"}, "highlight": {"type": "string"},
    "when_label": {"type": "string"}, "when": {"type": "string"},
    "hero_name": {"type": "string"}, "hero_value": {"type": "number"}, "hero_note": {"type": "string"},
    "ref_value": {"type": "number"}, "ref_label": {"type": "string"},
    "material": {"type": "string", "enum": ["ice", "snow", "ground", "water"]},
    "layer_label": {"type": "string"}, "below_label": {"type": "string"},
    "rivers": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "label": {"type": "string"}},
                                          "required": ["name", "label"]}},
    "show_rail": {"type": "boolean"}, "region": {"type": "string"}},
    "required": list(FIELDS)}}},
    "required": ["items"]}


PLAN_PROMPT = """You are the motion designer of a narrated YouTube video. Some scenes will show an animation INSTEAD of a picture, where it explains the moment better than a picture:
- "map": the narration names countries, a territory, a conquest, a journey, a trade route, a migration, borders, where something is ("a tiny island off the coast of Africa"), within one region.
- "globe": the place is far away or the story spans the world: places on different continents, a long journey or a route across oceans, "where on Earth" a remote place is (a city beyond the Arctic Circle, an island in the Pacific), a discovery in one country and its result in another.
- "timeline": several dates in a row, a chain of events ("in 1890... five years later... by 1936").
- "chart": numbers that change over time or several values to compare in one series (population grew from... to..., prices, army size by year).
- "compare": two sides head to head on one or more measures (Italy vs Ethiopia: soldiers, cannons, losses).
- "quote": a real, well-known quote said by a named person in the story, worth showing word for word.
- "counter": ONE striking number the narration says (a record, a total, a price, a distance, a toll): it counts up big on screen.
- "thermo": temperatures said in the narration (an average, then a record), or one value on a scale that rises or falls.
- "calendar": "N days a year", "N out of M" or "N percent" (days of storm, survivors out of a crew, a 68% chance): a grid of M cells where N light up.
- "photonum": one striking number about what the picture of that scene shows (a city's population, a depth, a height, a record): the number is written right over the picture.
- "place": the story moves to a new place or a new time ("Yakutsk, 2026", "Texas, April 2025"): a plate at the bottom of the picture.
- "document": an official decision, a court ruling, a letter, a will, a newspaper headline the narration retells or quotes.
- "scale": where the hero stands on one measure among places or things everyone knows (latitude among known cities, the height of a dinosaur among animals, the depth of a sea among others), often with a famous line (the Arctic Circle, sea level).
- "layer": the thickness or the depth of a layer next to a person for scale (ice 1.5 m thick, snow, permafrost, the depth of water).
- "satmap": a region's land itself matters: a great river through it, its vastness, how remote it is, a railway that does not reach a city (a dark relief map: rivers draw themselves, railways as a dashed line).
- "list": the narration counts points ("three reasons", "first... second... third...", a top).

Choose generously, about {limit} scenes: every moment where one of the kinds fits the narration gets one — every number said (counter, photonum), every city or country named (map or globe with pins of the cities), every date or chain of dates (timeline), every price or value that changed (chart), every "then and now" or "A against B" (compare), every decision, letter or headline (document), every count of points (list). The first half of the video is the most dynamic (the start keeps the viewers). Mix the kinds. Never two such scenes in a row; never the first or the last scene. Skip a moment only when nothing fits it.

Fill only the fields of the chosen kind; leave the others empty ("" / [] / 0, chart_style "line"):
- index: the scene number from the list. title: a short plate in {language}, max 5 words ("Эфиопия, 1896", "Армии при Адуа").
- map: countries = modern countries to highlight (English names), the closest modern equivalent of the place in the story; route = 2-6 stops in order with real coordinates (lat, lon); pins = 0-4 places with real coordinates. Names in {language}, max 3 words.
- globe: the same fields as map (countries 0-3, route 0-5 stops, pins 0-4, real coordinates); at least one place or country. Names in {language}, max 3 words; title = a short plate in {language}.
- timeline: events = 3-6 dates in order, label = the date as said ("1896", "XV век"), text = what happened, max 7 words, in {language}; current = the index of the event this scene is about.
- chart: points = 2-8 values in order, label = year or name (max 12 characters), value = the number; chart_style "line" for change over time, "bar" for separate items; unit = "%", "млн", "$", "чел." or "".
- compare: left_name and right_name = the two sides (max 2 words); rows = 1-4 measures, label = what is measured, left and right = the numbers; unit if the same for all rows.
- quote: quote = the exact words in {language}, max 30 words; author = who said it and when ("Менелик II, 1896").
- list: lines = 2-6 points, max 8 words each, in {language}.
- counter: value = the number as said (1.8 for "1.8 million tonnes"); unit = short, in {language} ("млн т", "km", "%", "$"); prefix "≈" if the narration says about/around, else ""; suffix "+" if it says "more than", else ""; caption = what the number is, max 6 words, in {language}; note = 0-4 words of context or ""; tone = "hot" for danger, records, losses, "cold" for cold, ice, distance, "warm" otherwise; title = the topic in 1-3 words.
- thermo: steps = 1-3 values in the order told, each: value, caption (max 5 words, in {language}), prefix ("≈" or ""), tone ("cold" for ordinary cold, "hot" for the extreme or heat); unit = "°C" (or the scale's unit); title = the topic in 1-3 words.
- photonum: the same fields as counter (value, unit, prefix, suffix, caption; no note).
- place: title = the place, max 4 words, in {language}; note = the year or the date, or "".
- document: org = who issued or wrote it (max 4 words, in {language}, or ""); headline = its gist, max 8 words, in {language}; highlight = the exact words the narration quotes from it, max 14 words, or ""; when_label = the word "When" in {language}; when = the date as said, or "".
- scale: points = 2-5 known items (label = name in {language}, max 3 words; value = the real number); hero_name, hero_value, hero_note (e.g. "69° с. ш.", max 3 words) = the hero; unit = "°", "м", "км" or ""; ref_value and ref_label = a famous line on that scale (66.56 and "Полярный круг"), or 0 and ""; title = the measure ("Северная широта").
- layer: value = the thickness in metres; unit = "м"/"m"; caption = what it is ("толщина льда"); material = ice, snow, ground or water; layer_label = the layer's name ("лёд"), below_label = what is under it ("вода"); title = the topic.
- satmap: countries = 0-2 countries for the outline (English names); rivers = 0-2 rivers, name = the English name ("Lena"), label = the name in {language}; show_rail = true only if the narration is about railways or their absence; pins = 1-4 towns with real coordinates, the most important first; region = the region's name in {language} in capitals-worthy form ("Якутия") or ""; title = the topic.
- calendar: total = M (365 for "a year", 100 for a percentage), filled = N; suffix "+" if "more than"; caption = what is counted, max 5 words, in {language}; legend = "= " + one cell's meaning, max 4 words; season = "winter" if those days are in winter, "summer" if in summer, else "even"; tone; title = the topic in 1-3 words.
Never invent facts or numbers: only what the narration says or certain common knowledge. If the numbers are not known for sure, do not make a chart or a comparison.

SCENES:
{scenes}"""


def plan_prompt(scenes: list, language: str, total_seconds: float) -> str:
    limit = max(3, min(40, round(total_seconds / 70)))
    rows = "\n".join(f"{i}. {sc.get('text', '')}" for i, sc in enumerate(scenes))
    return PLAN_PROMPT.format(limit=limit, language=language, scenes=rows)


def clean_plan(items: list, n_scenes: int) -> list:
    """Drops what cannot be drawn (unknown places, too few dates or numbers), the first and the last scene, neighbours."""
    out, used = [], set()
    for g in sorted(items or [], key=lambda x: int(x.get("index", -1)) if isinstance(x, dict) else -1):
        if not isinstance(g, dict):
            continue
        try:
            i = int(g.get("index"))
        except (TypeError, ValueError):
            continue
        if not 0 < i < n_scenes - 1 or i in used or i - 1 in used or i + 1 in used:
            continue
        if g.get("type") not in KINDS or not CHECK[g["type"]](g):
            continue
        out.append({k: g.get(k) for k in FIELDS})
        used.add(i)
    return out
