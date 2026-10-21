"""Image generation providers. Each takes a prompt and an orientation and returns image bytes."""
import base64
import time
import uuid

from . import net
from .net import UserError

# Shown in Settings. price = approximate USD per image.
PROVIDERS = {
    "fal": {
        "name": "fal.ai (Flux)",
        "key_field": "fal_key",
        "model_field": "fal_model",
        "signup": "https://fal.ai/dashboard/keys",
        "models": [
            ("fal-ai/flux/schnell", "Flux Schnell — очень дёшево, попроще", 0.003),
            ("fal-ai/flux/dev", "Flux Dev — хорошее качество (рекомендую)", 0.025),
            ("fal-ai/flux-pro/v1.1", "Flux 1.1 Pro — лучшее качество", 0.04),
        ],
    },
    "replicate": {
        "name": "Replicate (Flux)",
        "key_field": "replicate_key",
        "model_field": "replicate_model",
        "signup": "https://replicate.com/account/api-tokens",
        "models": [
            ("black-forest-labs/flux-schnell", "Flux Schnell — очень дёшево", 0.003),
            ("black-forest-labs/flux-dev", "Flux Dev — хорошее качество", 0.025),
            ("black-forest-labs/flux-1.1-pro", "Flux 1.1 Pro — лучшее качество", 0.04),
        ],
    },
    "runware": {
        "name": "Runware (Flux, самый дешёвый)",
        "key_field": "runware_key",
        "model_field": "runware_model",
        "signup": "https://my.runware.ai/keys",
        "models": [
            ("runware:100@1", "Flux Schnell — меньше цента за картинку", 0.0013),
            ("runware:101@1", "Flux Dev — хорошее качество, дёшево", 0.004),
        ],
    },
    "together": {
        "name": "Together AI (Flux)",
        "key_field": "together_key",
        "model_field": "together_model",
        "signup": "https://api.together.ai/settings/api-keys",
        "models": [
            ("black-forest-labs/FLUX.1-schnell", "Flux Schnell — дёшево", 0.003),
            ("black-forest-labs/FLUX.1-dev", "Flux Dev — хорошее качество", 0.025),
        ],
    },
    "openai": {
        "name": "OpenAI (как в ChatGPT)",
        "key_field": "openai_key",
        "model_field": "openai_image_model",
        "signup": "https://platform.openai.com/api-keys",
        "models": [
            ("gpt-image-1-mini", "GPT Image mini — дешевле", 0.015),
            ("gpt-image-1", "GPT Image — как в ChatGPT", 0.06),
        ],
    },
}


def price_per_image(settings) -> float:
    p = PROVIDERS.get(settings.get("image_provider"), PROVIDERS["fal"])
    model = settings.get(p["model_field"])
    for mid, _, price in p["models"]:
        if mid == model:
            return price
    return p["models"][0][2]


def generate(settings, prompt: str, vertical: bool = False) -> bytes:
    provider = settings.get("image_provider") or "fal"
    p = PROVIDERS.get(provider)
    if not p:
        raise UserError(f"Неизвестный сервис картинок: {provider}")
    key = settings.get(p["key_field"])
    if not key:
        raise UserError(f"Не указан ключ для {p['name']}. Вставьте его в Настройках, раздел «Картинки».")
    model = settings.get(p["model_field"]) or p["models"][0][0]
    if provider == "openai":
        return _openai(key, model, prompt, vertical, settings.get("openai_image_quality") or "medium")
    fn = {"fal": _fal, "replicate": _replicate, "runware": _runware, "together": _together, "openai": _openai}[provider]
    return fn(key, model, prompt, vertical)


def _download(service, url) -> bytes:
    return net.request(service, "GET", url, timeout=120).content


def _fal(key, model, prompt, vertical):
    body = {
        "prompt": prompt,
        "image_size": "portrait_16_9" if vertical else "landscape_16_9",
        "num_images": 1,
        "enable_safety_checker": True,
        "output_format": "png",
    }
    r = net.request("fal.ai", "POST", f"https://fal.run/{model}",
                    headers={"Authorization": f"Key {key}"}, json=body, timeout=300)
    data = r.json()
    images = data.get("images") or []
    if not images:
        raise UserError(f"fal.ai не вернул картинку: {str(data)[:300]}")
    if (data.get("has_nsfw_concepts") or [False])[0]:
        raise UserError("fal.ai: картинка заблокирована фильтром безопасности")
    url = images[0]["url"]
    if url.startswith("data:"):
        return base64.b64decode(url.split(",", 1)[1])
    return _download("fal.ai", url)


def _replicate(key, model, prompt, vertical):
    headers = {"Authorization": f"Bearer {key}", "Prefer": "wait=60"}
    body = {"input": {"prompt": prompt, "aspect_ratio": "9:16" if vertical else "16:9",
                      "output_format": "png", "num_outputs": 1}}
    if "1.1-pro" in model:
        body["input"].pop("num_outputs")
    r = net.request("Replicate", "POST", f"https://api.replicate.com/v1/models/{model}/predictions",
                    headers=headers, json=body, timeout=300)
    data = r.json()
    deadline = time.time() + 300
    while data.get("status") not in ("succeeded", "failed", "canceled"):
        if time.time() > deadline:
            raise UserError("Replicate слишком долго генерирует картинку")
        time.sleep(2)
        data = net.request("Replicate", "GET", data["urls"]["get"],
                           headers={"Authorization": f"Bearer {key}"}, timeout=60).json()
    if data["status"] != "succeeded":
        raise UserError(f"Replicate: не получилось ({data.get('error')})")
    out = data.get("output")
    url = out[0] if isinstance(out, list) else out
    return _download("Replicate", url)


def _runware(key, model, prompt, vertical):
    w, h = (768, 1344) if vertical else (1344, 768)
    task = [{
        "taskType": "imageInference",
        "taskUUID": str(uuid.uuid4()),
        "positivePrompt": prompt,
        "model": model,
        "width": w, "height": h,
        "numberResults": 1,
        "outputType": "URL",
        "outputFormat": "PNG",
    }]
    r = net.request("Runware", "POST", "https://api.runware.ai/v1",
                    headers={"Authorization": f"Bearer {key}"}, json=task, timeout=300)
    data = r.json()
    if data.get("errors"):
        raise UserError(f"Runware: {str(data['errors'])[:300]}")
    items = data.get("data") or []
    if not items or not items[0].get("imageURL"):
        raise UserError(f"Runware не вернул картинку: {str(data)[:300]}")
    return _download("Runware", items[0]["imageURL"])


def _together(key, model, prompt, vertical):
    w, h = (768, 1344) if vertical else (1344, 768)
    body = {"model": model, "prompt": prompt, "width": w, "height": h, "n": 1,
            "steps": 4 if "schnell" in model.lower() else 28, "response_format": "b64_json"}
    r = net.request("Together AI", "POST", "https://api.together.xyz/v1/images/generations",
                    headers={"Authorization": f"Bearer {key}"}, json=body, timeout=300)
    data = r.json().get("data") or []
    if not data:
        raise UserError("Together AI не вернул картинку")
    if data[0].get("b64_json"):
        return base64.b64decode(data[0]["b64_json"])
    return _download("Together AI", data[0]["url"])


def _openai(key, model, prompt, vertical, quality="medium"):
    body = {"model": model, "prompt": prompt, "size": "1024x1536" if vertical else "1536x1024",
            "quality": quality, "n": 1}
    r = net.request("OpenAI Images", "POST", "https://api.openai.com/v1/images/generations",
                    headers={"Authorization": f"Bearer {key}"}, json=body, timeout=300)
    data = r.json().get("data") or []
    if not data:
        raise UserError("OpenAI не вернул картинку")
    if data[0].get("b64_json"):
        return base64.b64decode(data[0]["b64_json"])
    return _download("OpenAI", data[0]["url"])


REFERENCE_MODEL = "fal-ai/nano-banana/edit"
MAX_REFERENCE_IMAGES = 3  # the editing model works best with a few pictures: one per character
MAX_ITEM_IMAGES = 2  # and one per item, on top of the characters
REFERENCE_PRICE = 0.04  # approximate USD per image


def _sheet(paths: list):
    """Puts several photos of one character side by side on a white sheet."""
    from PIL import Image
    ims = [Image.open(p).convert("RGB") for p in paths[:9]]
    if len(ims) == 1:
        return ims[0]
    cols = 2 if len(ims) <= 4 else 3
    rows = -(-len(ims) // cols)
    cell = 512
    sheet = Image.new("RGB", (cols * cell, rows * cell), "white")
    for i, im in enumerate(ims):
        im.thumbnail((cell - 8, cell - 8))
        x, y = (i % cols) * cell, (i // cols) * cell
        sheet.paste(im, (x + (cell - im.width) // 2, y + (cell - im.height) // 2))
    return sheet


def generate_with_reference(settings, prompt: str, refs: list, vertical: bool = False, square: bool = False,
                            place=None, items: list = None) -> bytes:
    """A new picture where the characters look like their portraits and the items like their photos
    (fal.ai, image editing model). Order of the pictures: the place, one sheet per character, one per item."""
    from PIL import Image
    import io
    key = settings.get("fal_key")
    if not key:
        raise UserError("Для картинок по портрету нужен ключ fal.ai (Настройки → Картинки).")
    refs = list(refs[:MAX_REFERENCE_IMAGES])
    items = list((items or [])[:MAX_ITEM_IMAGES])
    pics = ([place] if place else []) + refs + items
    urls = []
    for pic in pics:
        # A list is several photos of one character or item (front, side, back...): they go in as one sheet.
        im = _sheet(pic) if isinstance(pic, (list, tuple)) else Image.open(pic).convert("RGB")
        im.thumbnail((1536, 1536))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
        urls.append("data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode())

    def which(start, n):
        if len(pics) == 1:
            return "The reference image shows"
        return (f"Reference image {start} shows" if n == 1 else f"Reference images {start}-{start + n - 1} show")

    parts = [f"Create a new illustration: {prompt}"]
    n = 1
    if place:
        parts.append(f"{which(n, 1)} the PLACE where this happens: keep exactly the same location, layout, colours, "
                     "furniture, objects and light, seen from the camera angle and distance described; move the camera "
                     "freely as described and never copy the viewpoint of the reference picture."
                     + ("" if refs or items else " Add the people and action described."))
        n += 1
    if refs:
        parts.append(f"{which(n, len(refs))} the character(s): they must look exactly like them (same face, hair, age, "
                     "build and typical clothing). A reference can be a sheet with several photos of one character from "
                     "different sides (front, profile, back, details): use all of them so every detail stays the same. "
                     "Draw each character only once and do not copy their reference pose or background.")
        n += len(refs)
    if items:
        parts.append(f"{which(n, len(items))} the exact item(s) or product(s) that appear in the scene: reproduce them "
                     "exactly as in the photos (same shape, proportions, colours, materials, logo, labels and text on "
                     "them), at a realistic size, placed and used as described. A reference can be a sheet with several "
                     "photos of one item from different sides. Draw each item only once and do not copy the photo's "
                     "background.")
    parts.append("No text, no letters, no watermark" + (" apart from what is printed on the items themselves." if items else "."))
    body = {
        "prompt": " ".join(parts),
        "image_urls": urls,
        "num_images": 1,
        "aspect_ratio": "1:1" if square else "9:16" if vertical else "16:9",
        "output_format": "png",
    }
    r = net.request("fal.ai", "POST", f"https://fal.run/{REFERENCE_MODEL}",
                    headers={"Authorization": f"Key {key}"}, json=body, timeout=300)
    data = r.json()
    imgs = data.get("images") or []
    if not imgs:
        raise UserError(f"fal.ai не вернул картинку по портрету: {str(data)[:300]}")
    url = imgs[0]["url"]
    if url.startswith("data:"):
        return base64.b64decode(url.split(",", 1)[1])
    return _download("fal.ai", url)
