"""Learning from the channel's own numbers: YouTube statistics of every published video, and per project
short rules about what works and what doesn't, which the scriptwriter then follows.

Statistics live in each job folder (stats.json); the rules of a project in projects/<pid>/lessons.json."""
import json
import re
import statistics
import threading
import time
import traceback

from . import db, llm, net, pipeline, projects, youtube
from .config import load_settings
from .net import UserError

ANALYTICS_API = "https://youtubeanalytics.googleapis.com/v2/reports"
MIN_VIDEOS = 10          # videos with numbers needed before the first rules
MIN_AGE_DAYS = 3         # a video needs a few days before its numbers mean something
RELEARN_AFTER = 3        # new videos with numbers before the rules are refreshed by themselves
REFRESH_HOURS = 6
_lock = threading.Lock()
status = {"last_run": 0, "error": ""}


# ---------------------------------------------------------------- published videos

def _published(job: dict) -> list:
    """[(kind, video_id, channel_id, uploaded_at)] of a job's videos that are on YouTube."""
    up = pipeline.read_json(pipeline.job_dir(job["id"]) / "upload.json", {}) or {}
    out = []
    for kind, st in up.items():
        if st.get("status") == "done" and st.get("video_id"):
            out.append((kind, st["video_id"], st.get("channel_id", ""), st.get("uploaded_at") or job.get("created", 0)))
    return out


def video_id_from(text: str) -> str:
    text = (text or "").strip()
    m = re.search(r"(?:youtu\.be/|v=|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})", text)
    if m:
        return m.group(1)
    return text if re.fullmatch(r"[A-Za-z0-9_-]{11}", text) else ""


def link_manual(jid: str, kind: str, link: str):
    """The owner uploaded the video himself: remember its link so its numbers are collected too."""
    vid = video_id_from(link)
    if not vid:
        raise UserError("Не похоже на ссылку на ролик YouTube. Пример: https://youtube.com/shorts/abc123DEF45")
    path = pipeline.job_dir(jid) / "upload.json"
    up = pipeline.read_json(path, {}) or {}
    url = f"https://youtube.com/shorts/{vid}" if "/shorts/" in link or kind == "short" else f"https://youtu.be/{vid}"
    up[kind] = {**up.get(kind, {}), "status": "done", "video_id": vid, "url": url, "manual": True,
                "uploaded_at": up.get(kind, {}).get("uploaded_at") or time.time(), "error": ""}
    pipeline.write_json(path, up)


# ---------------------------------------------------------------- collecting numbers

def _channels() -> list:
    return [c["id"] for c in youtube.list_channels() if not c.get("needs_login")]


def _public_stats(token: str, ids: list) -> dict:
    out = {}
    for i in range(0, len(ids), 50):
        r = net.request("YouTube", "GET", f"{youtube.API}/videos", tries=2,
                        params={"part": "statistics,snippet,contentDetails", "id": ",".join(ids[i:i + 50])},
                        headers={"Authorization": f"Bearer {token}"}).json()
        for it in r.get("items") or []:
            s = it.get("statistics") or {}
            out[it["id"]] = {"views": int(s.get("viewCount", 0)), "likes": int(s.get("likeCount", 0)),
                             "comments": int(s.get("commentCount", 0)),
                             "published_at": (it.get("snippet") or {}).get("publishedAt", ""),
                             "channel_id": (it.get("snippet") or {}).get("channelId", "")}
    return out


def _analytics(token: str, ids: list) -> dict:
    """Watch time and retention; only the channel's own token can read them."""
    out = {}
    today = time.strftime("%Y-%m-%d")
    for i in range(0, len(ids), 100):
        r = net.request("YouTube Analytics", "GET", ANALYTICS_API, tries=2, params={
            "ids": "channel==MINE", "startDate": "2005-01-01", "endDate": today, "dimensions": "video",
            "metrics": "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained,shares",
            "filters": "video==" + ",".join(ids[i:i + 100]), "maxResults": 200,
        }, headers={"Authorization": f"Bearer {token}"}).json()
        cols = [c["name"] for c in r.get("columnHeaders") or []]
        for row in r.get("rows") or []:
            d = dict(zip(cols, row))
            out[d["video"]] = {"avg_view_pct": round(float(d.get("averageViewPercentage") or 0), 1),
                               "avg_view_sec": round(float(d.get("averageViewDuration") or 0), 1),
                               "minutes_watched": round(float(d.get("estimatedMinutesWatched") or 0), 1),
                               "subs": int(d.get("subscribersGained") or 0), "shares": int(d.get("shares") or 0)}
    return out


def collect() -> dict:
    """Fetches fresh numbers for every published video. Returns {"videos": n, "errors": [...]}."""
    with _lock:
        jobs = [j for j in db.list_all() if j.get("status") == "done"]
        wanted = {}   # video_id -> (job_id, kind, channel_id)
        for j in jobs:
            for kind, vid, cid, _ in _published(j):
                wanted[vid] = (j["id"], kind, cid)
        chans = _channels()
        errors = []
        if not wanted or not chans:
            status.update(last_run=time.time(), error="" if wanted else "")
            return {"videos": 0, "errors": [] if chans else ["Не подключён ни один канал YouTube."]}
        ids = list(wanted)
        public, deep = {}, {}
        for cid in chans:
            try:
                tok = youtube._token(cid)
                missing = [v for v in ids if v not in public]
                if missing:
                    public.update(_public_stats(tok, missing))
                mine = [v for v in ids if public.get(v, {}).get("channel_id") == cid and v not in deep]
                if mine:
                    deep.update(_analytics(tok, mine))
            except UserError as e:
                msg = str(e)
                if "403" in msg or "insufficient" in msg.lower() or "permission" in msg.lower():
                    msg = ("Нет доступа к статистике канала. Включите «YouTube Analytics API» в Google Cloud и "
                           "переподключите канал в Настройках → YouTube (нужна галочка на просмотр статистики).")
                errors.append(msg)
        now = time.time()
        per_job = {}
        for vid, (jid, kind, _) in wanted.items():
            if vid not in public:
                continue
            per_job.setdefault(jid, {})[kind] = {**public[vid], **deep.get(vid, {}), "video_id": vid, "fetched_at": now}
        for jid, data in per_job.items():
            path = pipeline.job_dir(jid) / "stats.json"
            old = pipeline.read_json(path, {}) or {}
            pipeline.write_json(path, {**old, **data})
        status.update(last_run=now, error="; ".join(dict.fromkeys(errors)))
        return {"videos": sum(len(v) for v in per_job.values()), "errors": list(dict.fromkeys(errors))}


# ---------------------------------------------------------------- what works: per project

def _lessons_file(pid: str):
    projects._path(pid)  # checks the id
    return projects.PROJ_DIR / pid / "lessons.json"


def get_lessons(pid: str) -> dict:
    return pipeline.read_json(_lessons_file(pid), {}) or {}


def active_rules(pid: str) -> list:
    return [r for r in get_lessons(pid).get("rules", []) if not r.get("off")]


def _age_days(st: dict) -> float:
    try:
        t = time.mktime(time.strptime(st["published_at"][:19], "%Y-%m-%dT%H:%M:%S"))
    except (KeyError, ValueError):
        t = st.get("fetched_at", time.time())
    return (time.time() - t) / 86400


def project_videos(pid: str) -> list:
    """Published videos of a project with their numbers and what they were made of."""
    out = []
    for j in db.list_all():
        if j["options"].get("project_id") != pid:
            continue
        d = pipeline.job_dir(j["id"])
        stats = pipeline.read_json(d / "stats.json", {}) or {}
        if not stats:
            continue
        review = pipeline.read_json(d / "review.json", {}) or {}
        extras = pipeline.read_json(d / "extras.json", {}) or {}
        short = pipeline.read_json(d / "short_script.json", {}) or {}
        idea = pipeline.read_json(d / "idea.json", {}) or {}
        chosen = (idea.get("ideas") or [{}])[idea.get("chosen_index", 0)] if idea.get("ideas") else {}
        voice = pipeline.read_json(d / "voice.json", {}) or {}
        for kind, st in stats.items():
            script = review.get("final_script", "") if kind == "video" else short.get("script", "")
            out.append({
                "job_id": j["id"], "kind": "shorts" if (kind == "short" or j["options"].get("format") == "short") else "long",
                "title": extras.get("youtube_title_ru") or extras.get("youtube_title") or j.get("title", ""),
                "title_en": extras.get("youtube_title", ""),
                "premise": chosen.get("premise_ru") or chosen.get("premise", ""),
                "hook": " ".join(re.split(r"(?<=[.!?…])\s+", script.strip())[:2]),
                "script": script,
                "seconds": round(voice.get("total", 0) if kind == "video" else voice.get("short_total", 0)),
                "words": pipeline.word_count(script),
                "characters": j["options"].get("characters", []),
                "episode": j["options"].get("episode", 0),
                "age_days": round(_age_days(st), 1),
                **{k: st.get(k) for k in ("views", "likes", "comments", "avg_view_pct", "avg_view_sec", "subs", "shares",
                                          "video_id", "published_at")},
            })
    return sorted(out, key=lambda v: v.get("published_at") or "", reverse=True)


def _score(videos: list) -> list:
    """Ranks videos inside their own format: views and how much of the video people watched count equally."""
    for fmt in ("shorts", "long"):
        group = [v for v in videos if v["kind"] == fmt]
        if not group:
            continue
        for key in ("views", "avg_view_pct"):
            vals = sorted(v.get(key) or 0 for v in group)
            for v in group:
                pos = vals.index(v.get(key) or 0)
                v[f"_{key}_rank"] = pos / max(1, len(vals) - 1)
        for v in group:
            v["score"] = round((v["_views_rank"] + v["_avg_view_pct_rank"]) / 2, 2)
    return videos


SCHEMA_LESSONS = {
    "type": "object",
    "properties": {
        "summary_ru": {"type": "string"},
        "rules": {"type": "array", "items": {"type": "object", "properties": {
            "rule": {"type": "string"}, "rule_ru": {"type": "string"}, "evidence_ru": {"type": "string"},
            "area": {"type": "string", "enum": ["topic", "hook", "story", "length", "title", "characters", "style"]},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]}}}},
    },
}


def ready(pid: str) -> dict:
    vids = [v for v in project_videos(pid) if v["age_days"] >= MIN_AGE_DAYS]
    return {"videos": len(vids), "need": MIN_VIDEOS, "ok": len(vids) >= MIN_VIDEOS}


def learn(pid: str, force: bool = False) -> dict:
    """Compares the best and the worst videos of the project and writes short rules for the scriptwriter."""
    proj = projects.get(pid)
    if not proj:
        raise UserError("Проект не найден")
    vids = _score([v for v in project_videos(pid) if v["age_days"] >= MIN_AGE_DAYS])
    if len(vids) < MIN_VIDEOS and not force:
        raise UserError(f"Пока мало данных: роликов со статистикой (старше {MIN_AGE_DAYS} дней) {len(vids)} из "
                        f"{MIN_VIDEOS}. Выводы на меньшем числе будут случайными.")
    if len(vids) < 4:
        raise UserError("Нужно хотя бы 4 опубликованных ролика со статистикой.")
    vids.sort(key=lambda v: v["score"], reverse=True)
    third = max(2, len(vids) // 3)
    best, worst = vids[:third], vids[-third:]
    old = get_lessons(pid)
    kept = [r for r in old.get("rules", []) if r.get("off")]

    def card(v):
        return (f"- [{v['kind']}] \"{v['title']}\" | views {v.get('views')} | watched {v.get('avg_view_pct')}% "
                f"(avg {v.get('avg_view_sec')} s of {v['seconds']} s) | likes {v.get('likes')} | subs +{v.get('subs')} "
                f"| {v['words']} words | age {v['age_days']} days\n  premise: {v['premise'][:300]}\n"
                f"  hook: {v['hook'][:300]}")
    medians = {k: statistics.median([v.get(k) or 0 for v in vids]) for k in ("views", "avg_view_pct")}
    system = ("You are a YouTube growth analyst. From the channel's own numbers you find concrete, testable patterns "
              "that separate its best videos from its worst, and turn them into short rules a scriptwriter can follow. "
              "Answer only with JSON.")
    user = (f"Channel niche: {proj.get('niche', '')}\nVideos analysed: {len(vids)} (median views {medians['views']}, "
            f"median watched {medians['avg_view_pct']}%).\n\nBEST VIDEOS:\n" + "\n".join(map(card, best))
            + "\n\nWORST VIDEOS:\n" + "\n".join(map(card, worst))
            + ("\n\nRules the owner switched off before (do not suggest them again):\n"
               + "\n".join(f"- {r['rule']}" for r in kept) if kept else "")
            + "\n\nWrite 3-8 rules. Each rule: what to do or avoid, specific enough to apply to the next script "
            "(topic type, kind of hook, story shape, length, title pattern, how the characters behave, visual style). "
            "Only rules the numbers really support: compare best and worst, ignore single outliers, and mark "
            "confidence honestly (low when it rests on 2-3 videos). rule: English, an instruction to the "
            "scriptwriter. rule_ru: the same in natural Russian for the owner. evidence_ru: in Russian, which "
            "videos and numbers show it. summary_ru: 2-4 Russian sentences on what works on this channel.")
    usage = {}
    r = llm.call_json(load_settings(), system, user, SCHEMA_LESSONS, usage, 8000)
    rules = [{**x, "id": f"r{int(time.time())}{i}", "off": False} for i, x in enumerate(r.get("rules") or [])
             if x.get("rule")]
    data = {"summary_ru": r.get("summary_ru", ""), "rules": rules + kept, "learned_at": time.time(),
            "videos": len(vids), "cost": round(usage.get("llm_cost", 0), 4),
            "best": [v["title"] for v in best], "worst": [v["title"] for v in worst]}
    pipeline.write_json(_lessons_file(pid), data)
    return data


def set_rule(pid: str, rid: str, off: bool = None, delete: bool = False):
    data = get_lessons(pid)
    rules = data.get("rules", [])
    if delete:
        rules = [r for r in rules if r.get("id") != rid]
    else:
        for r in rules:
            if r.get("id") == rid:
                r["off"] = bool(off)
    data["rules"] = rules
    pipeline.write_json(_lessons_file(pid), data)


def _auto_learn():
    """Refreshes the rules of a project when enough new videos have numbers."""
    for p in projects.list_all():
        pid = p["id"]
        try:
            st = ready(pid)
            if not st["ok"]:
                continue
            les = get_lessons(pid)
            if not les or st["videos"] - les.get("videos", 0) >= RELEARN_AFTER:
                learn(pid)
        except Exception:
            status["error"] = "Выводы: " + traceback.format_exc(limit=1).strip().splitlines()[-1]


def _loop():
    time.sleep(60)
    while True:
        try:
            if _channels():
                collect()
                _auto_learn()
        except Exception as e:
            status["error"] = str(e)
        time.sleep(REFRESH_HOURS * 3600)


def start():
    threading.Thread(target=_loop, daemon=True).start()
