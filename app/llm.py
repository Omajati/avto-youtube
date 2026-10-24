"""Text model calls (Anthropic Claude or OpenAI) that return JSON matching a schema."""
import copy
import json

import anthropic

from . import balance, net
from .net import UserError

# Approximate prices per 1M tokens (input, output) for the cost estimate shown in the UI.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.0, 50.0),
    "gpt-5": (1.25, 10.0),
    "gpt-5-mini": (0.25, 2.0),
}

FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1")


def strict_schema(schema: dict) -> dict:
    """Every object: all properties required, no extra properties (needed by structured outputs)."""
    s = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"].keys())
                node["additionalProperties"] = False
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(s)
    return s


def call_json(settings: dict, system: str, user: str, schema: dict, usage: dict, max_tokens: int = 32000,
              images: list = None) -> dict:
    """images: optional list of JPEG bytes the model should look at (e.g. photos of a character)."""
    schema = strict_schema(schema)
    provider = settings.get("llm_provider", "anthropic")
    if provider == "openai":
        return _openai(settings, system, user, schema, usage, max_tokens, images)
    return _anthropic(settings, system, user, schema, usage, max_tokens, images)


def _b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode()


def _add_usage(usage: dict, model: str, inp: int, out: int):
    usage["llm_input_tokens"] = usage.get("llm_input_tokens", 0) + inp
    usage["llm_output_tokens"] = usage.get("llm_output_tokens", 0) + out
    pin, pout = PRICES.get(model, (3.0, 15.0))
    cost = inp / 1e6 * pin + out / 1e6 * pout
    usage["llm_cost"] = round(usage.get("llm_cost", 0) + cost, 4)
    balance.record("anthropic" if model.startswith("claude") else "openai", cost)


def _anthropic(settings, system, user, schema, usage, max_tokens, images=None):
    key = settings.get("anthropic_key")
    if not key:
        raise UserError("Не указан ключ Anthropic (Claude). Вставьте его в Настройках, раздел «Сценарий».")
    model = settings.get("anthropic_model") or "claude-opus-5-5"
    # A blocked connection (no VPN) fails within seconds instead of hanging for many minutes.
    # The SDK's own Timeout class: newer versions are built on another HTTP library.
    timeout = anthropic.Timeout(900, connect=20) if hasattr(anthropic, "Timeout") else 900
    client = anthropic.Anthropic(api_key=key, max_retries=4, timeout=timeout)
    kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _b64(im)}}
            for im in images or []] + [{"type": "text", "text": user}]}],
        output_config={
            "effort": settings.get("anthropic_effort") or "high",
            "format": {"type": "json_schema", "schema": schema},
        },
    )
    for attempt in range(2):
        msg = _anthropic_once(client, kwargs, model)
        u = msg.usage
        _add_usage(usage, model, (u.input_tokens or 0), (u.output_tokens or 0))
        if msg.stop_reason != "max_tokens" or kwargs["max_tokens"] >= 64000:
            break
        # The model thought longer than usual (a big prompt, a hard topic): once more with twice the room.
        kwargs["max_tokens"] = min(kwargs["max_tokens"] * 2, 64000)
    if msg.stop_reason == "refusal":
        raise UserError("Claude отказался писать этот текст (сработал фильтр безопасности). Попробуйте другую тему.")
    if msg.stop_reason == "max_tokens":
        raise UserError("Claude не уложился в лимит длины ответа даже со второй попытки. Попробуйте ещё раз или уменьшите длительность ролика.")
    text = next((b.text for b in msg.content if getattr(b, "type", "") == "text"), "")
    return _parse(text, "Claude")


def _anthropic_once(client, kwargs, model):
    try:
        msg = None
        if model in FALLBACK_MODELS:
            try:
                # If the main model declines (e.g. a dark historical topic), the API retries on a fallback model.
                with client.beta.messages.stream(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
                ) as stream:
                    msg = stream.get_final_message()
            except anthropic.BadRequestError as e:
                if "fallback" not in str(e).lower():
                    raise
        if msg is None:
            with client.messages.stream(**kwargs) as stream:
                msg = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise UserError("Claude: ключ API не подошёл. Проверьте ключ Anthropic в Настройках.")
    except anthropic.PermissionDeniedError as e:
        raise UserError(f"Claude: нет доступа ({e}). Проверьте ключ и баланс на console.anthropic.com.")
    except anthropic.NotFoundError:
        raise UserError(f"Claude: модель «{model}» не найдена. Проверьте название модели в Настройках.")
    except anthropic.RateLimitError:
        raise UserError("Claude: слишком много запросов или закончился лимит. Подождите или проверьте баланс.")
    except anthropic.BadRequestError as e:
        text = str(e)
        if "credit" in text.lower() or "balance" in text.lower():
            raise UserError("Claude: на аккаунте закончились деньги. Пополните баланс на console.anthropic.com.")
        raise UserError(f"Claude отклонил запрос: {text[:400]}")
    except anthropic.APIConnectionError:
        raise UserError("Claude: нет связи с сервисом. Проверьте интернет и что включён VPN (из России Claude работает только через VPN).")
    except anthropic.APIStatusError as e:
        raise UserError(f"Claude: ошибка сервиса {e.status_code}: {str(e)[:300]}")
    return msg


def _openai(settings, system, user, schema, usage, max_tokens, images=None):
    key = settings.get("openai_key")
    if not key:
        raise UserError("Не указан ключ OpenAI. Вставьте его в Настройках, раздел «Сценарий».")
    model = settings.get("openai_model") or "gpt-5"
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _b64(im)}}
            for im in images or []] + [{"type": "text", "text": user}]}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "result", "strict": True, "schema": schema}},
        "max_completion_tokens": max_tokens,
    }
    r = net.request(
        "OpenAI", "POST", "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"}, json=body, timeout=900,
    )
    data = r.json()
    u = data.get("usage") or {}
    _add_usage(usage, model, u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
    choice = data["choices"][0]
    if choice.get("message", {}).get("refusal"):
        raise UserError("OpenAI отказался писать этот текст: " + choice["message"]["refusal"])
    if choice.get("finish_reason") == "length":
        raise UserError("OpenAI не уложился в лимит длины ответа. Попробуйте ещё раз.")
    return _parse(choice["message"].get("content") or "", "OpenAI")


def _parse(text: str, who: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    raise UserError(f"{who} вернул ответ не в том формате. Попробуйте запустить шаг ещё раз.")
