"""Local speech recognition for Apple Silicon through MLX Whisper.

The dependency and the model run on the owner's Mac.  Audio is never uploaded to
an API.  Models are cached inside the application data folder so they survive
program updates and can be used offline after the first download.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path

from . import media
from .config import DATA_DIR
from .net import UserError


MODELS = {
    "small": {
        "name": "Быстрый — Whisper Small",
        "repo": "mlx-community/whisper-small-mlx",
        "size": "≈ 481 МБ",
        "hint": "Быстрее; достаточно для чистой озвучки.",
    },
    "turbo": {
        "name": "Точный — Whisper Large v3 Turbo",
        "repo": "mlx-community/whisper-large-v3-turbo",
        "size": "≈ 1,61 ГБ",
        "hint": "Лучше распознаёт имена, шум и сложную речь.",
    },
}


def available() -> bool:
    return importlib.util.find_spec("mlx_whisper") is not None


def info() -> dict:
    return {"installed": available(), "models": MODELS, "cache": str(DATA_DIR / "models" / "huggingface")}


def _expose_ffmpeg() -> Path:
    """Make the app's bundled FFmpeg visible under the exact name MLX Whisper expects.

    A macOS app launched from Finder or the Dock does not inherit the shell's
    Homebrew PATH.  imageio-ffmpeg is still available, but its executable has a
    versioned filename, while mlx-whisper hard-codes ``ffmpeg``.  A tiny symlink
    inside application data bridges that mismatch without modifying the system.
    """
    executable = Path(media.ffmpeg()).resolve()
    bin_dir = DATA_DIR / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    alias = bin_dir / "ffmpeg"
    try:
        current = alias.resolve(strict=True) if alias.exists() or alias.is_symlink() else None
    except OSError:
        current = None
    if current != executable:
        alias.unlink(missing_ok=True)
        try:
            alias.symlink_to(executable)
        except OSError as exc:
            raise UserError(f"Не удалось подготовить встроенный FFmpeg для Whisper: {exc}") from exc
    path_parts = [part for part in os.environ.get("PATH", "").split(os.pathsep)
                  if part and part != str(bin_dir)]
    os.environ["PATH"] = os.pathsep.join([str(bin_dir), *path_parts])
    if shutil.which("ffmpeg") != str(alias):
        raise UserError("Whisper не смог подключить встроенный FFmpeg.")
    return alias


def transcribe(audio: Path, model: str = "small", language: str = "", on_status=None) -> tuple[str, list, str]:
    """Return ``(text, [(word, start, end)], detected_language)`` with word timestamps."""
    if not available():
        raise UserError("Локальный Whisper ещё не установлен. Обновите библиотеки программы и попробуйте снова.")
    chosen = MODELS.get(model) or MODELS["small"]
    cache = DATA_DIR / "models" / "huggingface"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(cache))
    _expose_ffmpeg()
    if on_status:
        on_status("При первом запуске загружаю модель; затем распознавание будет работать офлайн…")
    try:
        import mlx_whisper
        result = mlx_whisper.transcribe(
            str(audio),
            path_or_hf_repo=chosen["repo"],
            language=language or None,
            task="transcribe",
            word_timestamps=True,
            condition_on_previous_text=True,
            hallucination_silence_threshold=2.0,
            verbose=None,
        )
    except Exception as exc:
        msg = str(exc)
        if any(x in msg.lower() for x in ("connection", "network", "resolve", "offline", "timed out")):
            raise UserError("Не удалось загрузить модель Whisper. Проверьте интернет; модель нужна только один раз.") from exc
        raise UserError(f"Whisper не смог расшифровать запись: {msg[-350:]}") from exc
    words = []
    for segment in result.get("segments") or []:
        for item in segment.get("words") or []:
            word = str(item.get("word") or "").strip()
            if not word:
                continue
            try:
                start, end = float(item["start"]), float(item["end"])
            except (KeyError, TypeError, ValueError):
                continue
            words.append((word, round(max(start, 0), 3), round(max(end, start), 3)))
    text = str(result.get("text") or "").strip()
    if not words:
        raise UserError("Whisper не нашёл речи в этом файле.")
    if not text:
        text = " ".join(w[0] for w in words)
    if on_status:
        on_status("Расшифровка готова.")
    return text, words, str(result.get("language") or language or "")
