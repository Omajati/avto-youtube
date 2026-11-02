"""Image-to-video: turns a finished scene picture into a short moving clip (fal.ai, or RoyalTechno)."""
import base64
import io
import math
import time
import uuid
from pathlib import Path

from . import net
from .net import UserError

# price: dollars per generated second (approximate, fal.ai list prices, October 2026)
VIDEO_MODELS = {
    "ltx2": {
        "name": "LTX-2 — самый дешёвый, движение попроще",
        "endpoint": "fal-ai/ltx-2-19b/distilled/image-to-video",
        "price": 0.04,
    },
    "h3turbo": {
        "name": "MiniMax H3 Max Turbo 768p — почти вдвое дешевле Kling, живое движение",
        "endpoint": "minimax/h3-max-turbo/image-to-video",
        "resolution": "768P",
        "price": 0.04,
    },
    "h3turbo480": {
        "name": "MiniMax H3 Max Turbo 480p — самое дешёвое, картинка мягче",
        "endpoint": "minimax/h3-max-turbo/image-to-video",
        "resolution": "480P",
        "price": 0.025,
    },
    "kling25": {
        "name": "Kling 2.5 Turbo Pro — лучшая цена за качество (рекомендую)",
        "endpoint": "fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
        "price": 0.07,
    },
    "kling26": {
        "name": "Kling 2.6 Pro — новее, та же цена",
        "endpoint": "fal-ai/kling-video/v2.6/pro/image-to-video",
        "price": 0.07,
    },
    # RoyalTechno: a reseller (Telegram bot @royaltechno_ai_bot) with very low prices; its own key, see ROYAL below.
    "rt_veo": {
        "name": "RoyalTechno · Veo 3.1 (Google) — очень дёшево, сторонний сервис",
        "provider": "royal", "model": "veo-3.1", "seconds": 8,
        "price": 0.0025,  # $0.02 per 8-second clip
    },
    "rt_grok": {
        "name": "RoyalTechno · Grok Imagine 720p — сторонний сервис",
        "provider": "royal", "model": "grok-imagine-1.5-720p",
        "price": 0.02,
    },
    "rt_omni": {
        "name": "RoyalTechno · Omni 1.1 Flash — самое дешёвое, сторонний сервис",
        "provider": "royal", "model": "omni-flash",
        "price": 0.01,
    },
}
ROYAL = "https://api.royaltechno.cc/v1"

MODES = {
    "off": "Выключена",
    "hook": "Первые сцены (хук)",
    "key": "Начало и ключевые моменты",
    "custom": "Выбранные вручную сцены",
    "all": "Все сцены (дорого)",
}
COUNT_DEFAULT = 3


def clamp_count(v) -> int:
    try:
        return int(min(max(int(float(v)), 1), 60))
    except (TypeError, ValueError):
        return COUNT_DEFAULT

MOTION_PROMPT = (
    "Subtle, slow cinematic motion that brings the still image to life: gentle camera movement, "
    "natural small movements of people, clothing, hair, smoke, fire, water, clouds and light. "
    "Keep the exact composition, characters and art style. No text, no new objects, no sudden changes."
)


AD_MOTION = (
    "Smooth premium commercial camera movement: {move}. The product keeps its exact shape, proportions, colours, "
    "logo and labels the whole time. No people, no hands, no text, no new objects, no morphing, no sudden changes."
)


def ad_motion(move: str) -> str:
    return AD_MOTION.format(move=move or "slow dolly push-in toward the product")


TAKE_MOTION = (
    "One continuous smooth motion-control camera move{to_end}: {move}. Same set, same background and light; the "
    "product stands perfectly still in the same spot and keeps its exact shape, proportions, colours, logo and labels. "
    "No cuts, no people, no hands, no text, no new objects, no morphing."
)


def take_motion(move: str, to_end: bool) -> str:
    return TAKE_MOTION.format(move=move or "very slow push-in toward the product",
                              to_end=" from the first frame to the last frame" if to_end else "")


ORBIT_MOTION = (
    "Smooth cinematic motion-control orbit: the camera arcs slowly about 30 degrees around the product toward its "
    "{side} side, keeping the product centered, with a gentle push-in. The product itself is a rigid solid object: it "
    "stands perfectly still, it does not rotate, bend, melt, morph or change shape; every part, proportion, colour and "
    "detail stays exactly as in the first frame. The same set and background. Studio light glints slide across the "
    "surface. No new objects, no people, no hands, no text."
)
ORBIT_NEGATIVE = ("morphing, deformation, warping, melting, product changing shape, product rotating by itself, extra "
                  "parts, duplicated product, blur, low quality, text, watermark, hands")


def orbit_motion(side: str) -> str:
    return ORBIT_MOTION.format(side=side)


def model(settings) -> dict:
    return VIDEO_MODELS.get(settings.get("video_model"), VIDEO_MODELS["kling25"])


def clip_seconds(settings, need: float) -> float:
    """How many seconds to order for a scene that lasts `need` seconds (the rest is slowed down a little)."""
    m = settings.get("video_model")
    if m == "ltx2":
        return _ltx_frames(need) / 25
    if VIDEO_MODELS.get(m, {}).get("seconds"):
        return float(VIDEO_MODELS[m]["seconds"])
    return 5.0 if need <= 9.5 else 10.0  # a 5 s clip can be slowed down to ~9 s


def _ltx_frames(need: float) -> int:
    frames = min(max(need / 1.4 * 25, 81), 161)
    return int(round((frames - 1) / 8)) * 8 + 1


def cost(settings, need: float) -> float:
    return clip_seconds(settings, need) * model(settings)["price"]


def pick(mode: str, scenes: list, count: int, custom=None) -> list:
    """Indices of scenes to animate. `count` = how many scenes in total (hook / key modes)."""
    n = len(scenes)
    if mode == "all":
        return list(range(n))
    if mode == "custom":
        return sorted({int(i) for i in (custom or []) if 0 <= int(i) < n})
    if mode not in ("hook", "key") or n == 0:
        return []
    count = min(clamp_count(count), n)
    if mode == "hook":
        return list(range(count))
    # key: a third goes to the opening, the rest is spread over the story turns
    head = max(1, math.ceil(count / 3))
    chosen = list(range(head))
    left = count - head
    if left > 0:
        rest = [i for i in range(head, n)]
        # New paragraphs are where the story turns: prefer the first scene of a paragraph.
        starts = [i for i in rest if scenes[i - 1].get("paragraph_end")]
        pool = starts if len(starts) >= left else rest
        step = len(pool) / left
        chosen += [pool[int(k * step)] for k in range(left)]
    return sorted(set(chosen))


def _data_uri(img: Path) -> str:
    from PIL import Image
    im = Image.open(img).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def generate(settings, img: Path, prompt: str, need: float, vertical: bool, check=None, motion=None, tail=None,
             negative: str = None) -> bytes:
    """Returns mp4 bytes. tail: the picture the clip must end on (Kling 2.5 Turbo Pro is used for it)."""
    if VIDEO_MODELS.get(settings.get("video_model"), {}).get("provider") == "royal" and not tail:
        return _royal(settings, img, prompt, need, vertical, check, motion)
    key = settings.get("fal_key")
    if not key:
        raise UserError("Для анимации нужен ключ fal.ai. Вставьте его в Настройках, раздел «Картинки» → fal.ai.")
    mid = settings.get("video_model") if settings.get("video_model") in VIDEO_MODELS else "kling25"
    if tail:
        mid = "kling25"  # the model whose end frame works on fal.ai
    m = VIDEO_MODELS[mid]
    full = f"{prompt.strip().rstrip('.')}. {motion or MOTION_PROMPT}"
    image = _data_uri(img)
    if m.get("resolution"):  # MiniMax H3: the picture's shape comes from the image itself
        body = {"prompt": full, "image_url": image, "duration": clip_seconds(settings, need),
                "resolution": m["resolution"], "prompt_expansion_mode": "disabled", "enable_safety_checker": True}
    elif mid == "ltx2":
        body = {"prompt": full, "image_url": image, "num_frames": _ltx_frames(need), "fps": 25,
                "video_size": "portrait_16_9" if vertical else "landscape_16_9", "generate_audio": False}
        # No camera LoRA: fal.ai fails to load those weights at times (422 "Failed to load LoRA weights"), and the
        # motion prompt already asks for a slow camera move.
    else:
        body = {"prompt": full, "duration": "5" if clip_seconds(settings, need) <= 5 else "10",
                "negative_prompt": negative or "blur, distort, low quality, text, watermark, morphing faces"}
        if negative:
            body["cfg_scale"] = 0.7  # follow the prompt closely: the product must stay rigid
        if mid == "kling26":
            body.update(start_image_url=image, generate_audio=False)
        else:
            body["image_url"] = image
            if tail:
                body["tail_image_url"] = _data_uri(tail)
    headers = {"Authorization": f"Key {key}"}
    r = net.request("fal.ai", "POST", f"https://queue.fal.run/{m['endpoint']}", headers=headers, json=body, timeout=120)
    sub = r.json()
    status_url = sub.get("status_url") or f"https://queue.fal.run/{m['endpoint']}/requests/{sub['request_id']}/status"
    result_url = sub.get("response_url") or f"https://queue.fal.run/{m['endpoint']}/requests/{sub['request_id']}"
    # A clip that hangs in fal.ai's queue does not hold the whole video: if fal.ai has not started it in 2 minutes,
    # the request is cancelled (so it is not billed later) and the scene stays a picture. Once started, a clip
    # normally takes 1-3 minutes; after 6 the scene stays a picture too.
    started, queued_since = None, time.time()
    cancel_url = f"https://queue.fal.run/{m['endpoint']}/requests/{sub['request_id']}/cancel" if sub.get("request_id") else ""
    while True:
        if check:
            check()
        st = net.request("fal.ai", "GET", status_url, headers=headers, timeout=30, tries=2).json()
        status = st.get("status")
        if status == "COMPLETED":
            break
        if status not in ("IN_QUEUE", "IN_PROGRESS"):
            raise UserError(f"fal.ai: анимация не получилась ({str(st)[:300]})")
        if status == "IN_PROGRESS" and started is None:
            started = time.time()
        if started is None and time.time() - queued_since > 120:
            if cancel_url:
                try:
                    net.request("fal.ai", "PUT", cancel_url, headers=headers, timeout=20, tries=1)
                except UserError:
                    pass
            raise UserError("fal.ai 2 минуты не брал сцену в работу (у них очередь), запрос отменён")
        if started is not None and time.time() - started > 6 * 60:
            raise UserError("fal.ai делал сцену дольше 6 минут")
        time.sleep(4)
    out = net.request("fal.ai", "GET", result_url, headers=headers, timeout=120).json()
    url = ((out or {}).get("video") or {}).get("url")
    if not url:
        detail = (out or {}).get("detail") or out
        raise UserError(f"fal.ai не вернул видео: {str(detail)[:300]}")
    if url.startswith("data:"):
        return base64.b64decode(url.split(",", 1)[1])
    data = net.request("fal.ai", "GET", url, timeout=300).content
    if len(data) < 1000:
        raise UserError("fal.ai вернул пустое видео. Попробуйте ещё раз.")
    return data


def _royal(settings, img: Path, prompt: str, need: float, vertical: bool, check=None, motion=None) -> bytes:
    """RoyalTechno: one job with the scene picture as the first frame; polled until the mp4 is ready."""
    key = settings.get("royal_key")
    if not key:
        raise UserError("Для анимации через RoyalTechno нужен ключ: создайте его в их Телеграм-боте @royaltechno_ai_bot "
                        "командой /api_key_new и вставьте в Настройках, раздел «Анимация сцен».")
    m = VIDEO_MODELS[settings["video_model"]]
    headers = {"Authorization": f"Bearer {key}"}
    inp = {"prompt": f"{prompt.strip().rstrip('.')}. {motion or MOTION_PROMPT}", "start_image_url": _data_uri(img),
           "aspect_ratio": "9:16" if vertical else "16:9", "duration_sec": int(clip_seconds(settings, need))}
    # One id per clip: a retried request after a network hiccup is not charged twice.
    idem = str(uuid.uuid4())
    try:
        job = net.request("RoyalTechno", "POST", f"{ROYAL}/jobs", json={"model": m["model"], "input": inp},
                          headers={**headers, "Idempotency-Key": idem}, timeout=120, tries=3).json()
    except UserError as e:
        if "invalid" not in str(e).lower():
            raise
        inp.pop("duration_sec")  # the model has its own fixed length: take it, the assembly fits it to the scene
        job = net.request("RoyalTechno", "POST", f"{ROYAL}/jobs", json={"model": m["model"], "input": inp},
                          headers={**headers, "Idempotency-Key": str(uuid.uuid4())}, timeout=120, tries=3).json()
    jid = job.get("id")
    if not jid:
        raise UserError(f"RoyalTechno не принял сцену: {str(job.get('error') or job)[:300]}")
    started = time.time()
    while True:
        if check:
            check()
        st = net.request("RoyalTechno", "GET", f"{ROYAL}/jobs/{jid}", headers=headers, timeout=30, tries=3).json()
        status = st.get("status")
        if status == "succeeded":
            break
        if status in ("failed", "canceled"):
            err = st.get("error") or {}
            raise UserError(f"RoyalTechno: анимация не получилась ({err.get('message') or err or status}). "
                            "Деньги за неё они возвращают сами.")
        if time.time() - started > 8 * 60:
            try:
                net.request("RoyalTechno", "POST", f"{ROYAL}/jobs/{jid}/cancel", headers=headers, timeout=20, tries=1)
            except UserError:
                pass
            raise UserError("RoyalTechno делал сцену дольше 8 минут, запрос отменён")
        time.sleep(8)
    url = ((st.get("output") or {}).get("url")) or ""
    if not url:
        raise UserError(f"RoyalTechno не вернул видео: {str(st)[:300]}")
    data = net.request("RoyalTechno", "GET", url, timeout=300).content
    if len(data) < 1000:
        raise UserError("RoyalTechno вернул пустое видео. Попробуйте ещё раз.")
    return data


def provider(settings) -> str:
    """Which service the animation is billed at: "royaltechno" or "fal"."""
    return "royaltechno" if model(settings).get("provider") == "royal" else "fal"


def estimate(settings, mode: str, minutes: float, short: bool, anim_short: bool, vertical: bool = False,
             count: int = COUNT_DEFAULT) -> float:
    """Rough cost of animation for a new video, before the script exists."""
    words_per_min = 150
    key = "short_words_per_scene" if vertical else "words_per_scene"
    scene_sec = float(settings.get(key, 22)) / words_per_min * 60 + 0.4
    n_scenes = max(1, math.ceil(minutes * 60 / scene_sec))
    scenes = [{"paragraph_end": (i % 4 == 3)} for i in range(n_scenes)]
    # manual choice is made later on the video page; until then count it like "first scenes"
    idx = pick("hook" if mode == "custom" else mode, scenes, count)
    total = len(idx) * cost(settings, scene_sec)
    if short and anim_short and mode != "off":
        s_sec = float(settings.get("short_words_per_scene", 12)) / words_per_min * 60 + 0.2
        total += min(clamp_count(count), math.ceil(50 / s_sec)) * cost(settings, s_sec)
    return round(total, 2)
