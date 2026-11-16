"""Fast build: still frames as they are, narration, music, subtitles - glued in one encoding pass.

No camera moves, colour looks, transitions, graphics or sound effects. Each scene becomes a short normalised
segment (a still compresses to almost nothing, so this takes well under a second per scene); the segments are then
read through the concat demuxer straight into one final pass that burns the subtitles, mixes the sound and encodes
with the Mac's hardware H.264 encoder.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import captions, media
from .net import UserError

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".avif", ".heic"}
WIDTH, HEIGHT = 1920, 1080


class Cancelled(Exception):
    pass


def _cover(width: int, height: int) -> str:
    return (f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={width}:{height},setsar=1")


def _segment(source: Path, frames: int, fps: int, out: Path):
    """One scene as an H.264 segment. A picture is a single frame: the concat list gives it its duration and the
    final pass repeats it, so a 20-second still costs one encoded frame. A clip has exactly `frames` frames.
    All segments share the same codec settings, so the concat demuxer reads them one after another."""
    common = ["-an", "-r", fps, "-c:v", "libx264", "-preset", "veryfast", "-crf", 17, "-pix_fmt", "yuv420p",
              "-profile:v", "high", "-video_track_timescale", fps * 1000]
    if source.suffix.lower() in IMAGE_EXT:
        args = ["-i", source, "-vf", _cover(WIDTH, HEIGHT), "-frames:v", 1]
    else:
        # A clip shorter than its scene holds its last frame; a longer one is cut.
        args = ["-i", source, "-vf", f"{_cover(WIDTH, HEIGHT)},fps={fps},tpad=stop_mode=clone:stop_duration={frames / fps + 1:.3f}",
                "-frames:v", frames]
    tmp = out.with_name(f".{out.stem}.tmp.mp4")
    media.run([*args, *common, tmp], "Сцена быстрой сборки")
    tmp.replace(out)


def _frame_counts(durations: list[float], fps: int) -> list[int]:
    """Frame counts from cumulative times, so 300 rounded scenes never drift away from the voice."""
    counts, elapsed, done = [], 0.0, 0
    for duration in durations:
        elapsed += max(float(duration), 0)
        end = round(elapsed * fps)
        counts.append(end - done)
        done = end
    return counts


def _audio_graph(has_music: bool, music_volume: float, total: float) -> tuple[str, str]:
    voice = f"[1:a]aformat=sample_rates=44100:channel_layouts=stereo,apad,atrim=0:{total:.3f}"
    if not has_music:
        return voice + ",loudnorm=I=-14:LRA=11:TP=-1.5,alimiter=limit=0.95,aresample=44100[a]", "[a]"
    return ";".join([
        voice + ",asplit=2[voice][side]",
        f"[2:a]aformat=sample_rates=44100:channel_layouts=stereo,volume={music_volume:.3f},atrim=0:{total:.3f}[mb]",
        "[mb][side]sidechaincompress=threshold=0.025:ratio=10:attack=20:release=350[music]",
        "[voice][music]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-14:LRA=11:TP=-1.5,alimiter=limit=0.95,aresample=44100[a]",
    ]), "[a]"


def _encoders() -> list[list]:
    hardware = ["-c:v", "h264_videotoolbox", "-b:v", "10M", "-maxrate", "14M", "-bufsize", "20M",
                "-profile:v", "high", "-allow_sw", "1"]
    software = ["-c:v", "libx264", "-preset", "veryfast", "-crf", 20]
    return [hardware, software]


def render(work: Path, out: Path, sources: list[Path], durations: list[float], narration: Path, words: list,
           music: Path | None = None, music_volume: float = .12, subtitles: bool = True, sub_style: dict | None = None,
           fps: int = 30, parallel: int = 4, on_progress=None, cancelled=None) -> Path:
    """sources[i] is shown for durations[i] seconds. Returns `out`. on_progress(fraction, text)."""
    if not sources or len(sources) != len(durations):
        raise UserError("Быстрая сборка: нет сцен или их число не совпадает с озвучкой.")
    report = on_progress or (lambda *_: None)
    stop = cancelled or (lambda: False)
    work.mkdir(parents=True, exist_ok=True)
    segments_dir = work / "segments"
    segments_dir.mkdir(exist_ok=True)
    frames = _frame_counts(durations, fps)
    total = sum(frames) / fps
    # Scenes with no time (a silent preview card) are skipped; the same picture held twice is encoded once.
    jobs: dict[str, tuple[Path, int, Path]] = {}
    order: list[tuple[Path, int]] = []
    for source, count in zip(sources, frames):
        if count <= 0:
            continue
        stat = source.stat()
        still = source.suffix.lower() in IMAGE_EXT
        key = hashlib.sha256(json.dumps([str(source), stat.st_size, stat.st_mtime_ns, 0 if still else count, fps, WIDTH, HEIGHT, 3])
                             .encode()).hexdigest()[:20]
        seg = segments_dir / f"{key}.mp4"
        jobs[key] = (source, count, seg)
        order.append((seg, count))
    todo = [job for job in jobs.values() if not (job[2].is_file() and job[2].stat().st_size > 500)]
    done = len(jobs) - len(todo)
    report(done / max(len(jobs), 1) * .25, "Готовлю кадры…")
    with ThreadPoolExecutor(max_workers=max(1, min(parallel, 6))) as pool:
        futures = [pool.submit(_segment, source, count, fps, seg) for source, count, seg in todo]
        try:
            for future in as_completed(futures):
                future.result()
                if stop():
                    raise Cancelled()
                done += 1
                report(done / max(len(jobs), 1) * .25, f"Готовлю кадры: {done} из {len(jobs)}")
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    listing = work / "scenes.txt"
    # «duration» places every next segment exactly; fps= in the final pass repeats a still until then.
    # The last segment is listed once more so the last still lasts to the end of the voice (-t cuts the rest).
    listing.write_text("".join(f"file '{seg.resolve()}'\nduration {count / fps:.6f}\n" for seg, count in order)
                       + f"file '{order[-1][0].resolve()}'\n", "utf-8")

    filters = [f"[0:v]setpts=PTS-STARTPTS,fps={fps}"]
    exe = None
    if subtitles and words:
        style = sub_style or {}
        ass = work / "subtitles.ass"
        fonts = captions.styled_ass(words, ass, WIDTH, HEIGHT, style.get("preset", "karaoke"), style.get("font", "auto"),
                                    style.get("size", "medium"), style.get("position", "mid_low"))
        fonts_arg = str(fonts.resolve()).replace("\\", "/").replace(":", "\\:")
        filters[0] += f",subtitles=filename={ass.name}:fontsdir={fonts_arg}"
        exe = media.ffmpeg_with_subtitles()
    filters[0] += ",format=yuv420p[v]"
    burn = exe is not None
    audio, audio_map = _audio_graph(bool(music), music_volume, total)
    inputs = ["-f", "concat", "-safe", 0, "-i", listing.resolve(), "-i", narration.resolve()]
    if music:
        inputs += ["-stream_loop", -1, "-i", Path(music).resolve()]
    tmp = out.with_name(f".{out.stem}.fast.tmp.mp4")
    last_error = ""
    # No subtitles: the video is not re-encoded at all, the ready segments are only glued; just the sound is made.
    variants = [(filters, "[v]", [*encoder, "-r", fps]) for encoder in _encoders()] if burn else [([], "0:v", ["-c:v", "copy"])]
    label = "Склеиваю, накладываю субтитры и звук…" if burn else "Склеиваю кадры и звук…"
    for video_filters, video_map, video_args in variants:
        args = [*inputs, "-filter_complex", ";".join(video_filters + [audio]), "-map", video_map, "-map", audio_map,
                *video_args, "-c:a", "aac", "-b:a", "192k", "-t", f"{total:.3f}", "-movflags", "+faststart",
                tmp.resolve()]
        try:
            _run_final(args, exe, work, total, lambda f: report(.25 + .75 * f, label), stop)
            tmp.replace(out)
            return out
        except UserError as exc:
            last_error = str(exc)
            tmp.unlink(missing_ok=True)
    raise UserError(last_error or "Быстрая сборка не удалась.")


def _run_final(args: list, exe: str | None, cwd: Path, total: float, progress, stop):
    """The long final pass, stoppable: a pause or stop kills FFmpeg instead of waiting for an hour of video."""
    process = subprocess.Popen([exe or media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-progress", "pipe:1",
                                "-nostats", *map(str, args)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, cwd=cwd)
    last = 0.0
    try:
        for line in process.stdout:
            if line.startswith("out_time_us=") and line.strip()[12:].isdigit():
                progress(min(int(line.strip()[12:]) / 1e6 / max(total, .1), 1.0))
            now = time.monotonic()
            if now - last > .5:
                last = now
                if stop():
                    process.kill()
                    process.wait()
                    raise Cancelled()
        err = process.stderr.read()
        if process.wait() != 0:
            raise UserError(f"Быстрая сборка: ошибка FFmpeg. {err[-600:]}")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


SAMPLE = {
    "en": "This is how the subtitles will look in your video",
    "ru": "Так будут выглядеть субтитры в вашем ролике",
    "de": "So sehen die Untertitel in deinem Video aus",
    "es": "Así se verán los subtítulos en tu vídeo",
}


def preview(background: Path | None, out: Path, style: dict, lang: str = "en", work: Path | None = None) -> Path:
    """One 1920×1080 frame with a sample subtitle line, drawn by the same code and FFmpeg as the final video."""
    work = work or out.parent
    work.mkdir(parents=True, exist_ok=True)
    text = SAMPLE.get(lang, SAMPLE["en"]).split()
    words, t = [], 0.0
    for word in text:
        words.append((word, t, t + .3, 0))
        t += .3
    cues = [(" ".join(text), 0.0, t + 1)]
    ass = work / "preview.ass"
    fonts = captions.styled_ass(words, ass, WIDTH, HEIGHT, style.get("preset", "karaoke"), style.get("font", "auto"),
                                style.get("size", "medium"), style.get("position", "mid_low"), cues=cues)
    fonts_arg = str(fonts.resolve()).replace("\\", "/").replace(":", "\\:")
    at = 1.0  # karaoke: a few words already lit, fades finished
    if background and background.is_file():
        if background.suffix.lower() in IMAGE_EXT:
            source = ["-loop", "1", "-framerate", 10, "-t", at + 1, "-i", background.resolve()]
        else:
            source = ["-ss", 1, "-t", at + 1, "-i", background.resolve()]
    else:
        source = ["-f", "lavfi", "-t", at + 1, "-i", f"gradients=s={WIDTH}x{HEIGHT}:c0=0x3a4a5c:c1=0x1b2027:r=10"]
    vf = (f"{_cover(WIDTH, HEIGHT)},fps=10,setpts=PTS-STARTPTS,subtitles=filename={ass.name}:fontsdir={fonts_arg},"
          f"trim=start={at},setpts=PTS-STARTPTS")
    tmp = out.with_name(f".{out.stem}.tmp.jpg")
    media.run([*source, "-vf", vf, "-frames:v", 1, "-q:v", 3, tmp.resolve()], "Предпросмотр субтитров",
              cwd=ass.parent, exe=media.ffmpeg_with_subtitles())
    tmp.replace(out)
    return out


def clear(work: Path):
    shutil.rmtree(work, ignore_errors=True)
