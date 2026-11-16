"""Fast build: still frames as they are, narration, music, subtitles.

The picture of such a video only changes when a scene or a subtitle line (or a karaoke word) changes, so only those
moments are encoded: a few thousand frames for an hour instead of ~100 000. Each still is scaled once into a
one-frame segment; a concat list repeats it at every change point; libass draws the subtitles on those frames only.
The sound is made at the same time in a separate process: loudness is measured once (fast) and set with a plain gain,
the music is a quiet bed, and the Apple AAC encoder does the encoding. At the end video and sound are only muxed.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import captions, media
from .net import UserError

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".avif", ".heic"}
WIDTH, HEIGHT = 1920, 1080
MAX_GAP = 2.0  # a still is repeated at least once a second: players and YouTube seek and process it normally
TARGET_LUFS = -14.0


class Cancelled(Exception):
    pass


def _cover(width: int, height: int) -> str:
    return (f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={width}:{height},setsar=1")


def _frame_counts(durations: list[float], fps: int) -> list[int]:
    """Frame counts from cumulative times, so 300 rounded scenes never drift away from the voice."""
    counts, elapsed, done = [], 0.0, 0
    for duration in durations:
        elapsed += max(float(duration), 0)
        end = round(elapsed * fps)
        counts.append(end - done)
        done = end
    return counts


_TIME = re.compile(r"(\d+):(\d+):(\d+(?:\.\d+)?)")


def _seconds(stamp: str) -> float:
    h, m, s = _TIME.match(stamp.strip()).groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def _static_subtitles(ass: Path) -> list[float]:
    """Removes fades and grow animations (they need every frame) and returns every moment the subtitles change:
    line starts and ends and, for karaoke, the moment each next word lights up."""
    points: list[float] = []
    lines = []
    for line in ass.read_text("utf-8").splitlines():
        if line.startswith("Dialogue:"):
            head, text = line.split(",,0,0,0,,", 1) if ",,0,0,0,," in line else (line, "")
            text = re.sub(r"\\fad\([^)]*\)|\\t\([^)]*\)|\\fscx\d+\\fscy\d+", "", text)
            fields = head.split(",")
            start, end = _seconds(fields[1]), _seconds(fields[2])
            points += [start, end]
            elapsed = 0
            for k in re.findall(r"\\k(\d+)", text):
                elapsed += int(k)
                if start + elapsed / 100 < end:
                    points.append(start + elapsed / 100)
            line = f"{head},,0,0,0,,{text}"
        lines.append(line)
    ass.write_text("\n".join(lines) + "\n", "utf-8")
    return points


def _keep_frames(start_frame: int, count: int, fps: int, points: list[float]) -> list[int]:
    """Frame numbers inside a still scene that must exist: its first frame, every change of the subtitles,
    and at least one a second (players and YouTube seek and process such a video normally)."""
    keep = {0}
    keep.update(range(0, count, max(1, round(MAX_GAP * fps))))
    for point in points:
        n = round(point * fps) - start_frame
        if 0 < n < count:
            keep.add(n)
    return sorted(keep)


def _scene(source: Path, start_frame: int, count: int, fps: int, points: list[float], subtitle_filter: str,
           exe: str | None, cwd: Path, out: Path):
    """One scene with its subtitles, ready to be glued with stream copy.
    A still is scaled once and only the frames where something changes are drawn and encoded;
    a clip keeps all its frames. The subtitles are drawn at the scene's own place in the video."""
    still = source.suffix.lower() in IMAGE_EXT
    to_absolute = f"setpts=(N+{start_frame})/{fps}/TB"
    if still:
        keep = _keep_frames(start_frame, count, fps, points)
        choose = "+".join(f"eq(n,{n})" for n in keep)
        vf = f"{_cover(WIDTH, HEIGHT)},loop=loop={count - 1}:size=1:start=0,{to_absolute},select='{choose}'"
        args = ["-i", source.resolve()]
    else:
        # A clip shorter than its scene holds its last frame; a longer one is cut.
        vf = (f"{_cover(WIDTH, HEIGHT)},fps={fps},tpad=stop_mode=clone:stop_duration={count / fps + 1:.3f},"
              f"trim=end_frame={count},{to_absolute}")
        args = ["-i", source.resolve()]
    if subtitle_filter:
        vf += "," + subtitle_filter
    vf += ",setpts=PTS-STARTPTS,format=yuv420p"
    tmp = out.with_name(f".{out.stem}.tmp.mp4")
    media.run([*args, "-vf", vf, "-an", "-fps_mode", "passthrough", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", 18, "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p",
               "-force_key_frames", "expr:gte(t,n_forced*2)", "-video_track_timescale", 90000, tmp.resolve()],
              "Сцена быстрой сборки", cwd=cwd, exe=exe)
    tmp.replace(out)


def _loudness(path: Path) -> float | None:
    """Integrated loudness (LUFS) of the narration: an analysis pass is a few seconds for an hour."""
    process = subprocess.run([media.ffmpeg(), "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128=framelog=quiet",
                              "-f", "null", "-"], capture_output=True, text=True)
    found = re.findall(r"I:\s+(-?\d+(?:\.\d+)?) LUFS", process.stderr)
    return float(found[-1]) if found else None


def _aac() -> list:
    """Apple's AAC encoder is ~4× faster here than FFmpeg's own."""
    probe = subprocess.run([media.ffmpeg(), "-hide_banner", "-encoders"], capture_output=True, text=True)
    return ["-c:a", "aac_at", "-b:a", "192k"] if " aac_at " in probe.stdout else ["-c:a", "aac", "-b:a", "192k"]


def _sound(narration: Path, music: Path | None, music_volume: float, total: float, out: Path):
    """The voice as it is: no mixing, no loudness changes, only packed into AAC (Apple's encoder)."""
    tmp = out.with_name(f".{out.stem}.tmp.m4a")
    media.run(["-i", narration.resolve(), *_aac(), "-t", f"{total:.3f}", tmp.resolve()], "Звук быстрой сборки")
    tmp.replace(out)


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

    # The sound does not depend on the picture: it is made meanwhile in its own process.
    audio = work / "sound.m4a"
    sound_error: list[Exception] = []

    def make_sound():
        try:
            _sound(narration, music, music_volume, total, audio)
        except Exception as exc:  # noqa: BLE001 - reported after the picture is ready
            sound_error.append(exc)

    sound_thread = threading.Thread(target=make_sound, daemon=True, name="fast-sound")
    sound_thread.start()

    points: list[float] = []
    subtitle_filter, exe = "", None
    if subtitles and words:
        style = sub_style or {}
        ass = work / "subtitles.ass"
        fonts = captions.styled_ass(words, ass, WIDTH, HEIGHT, "clean", style.get("font", "auto"),
                                    style.get("size", "medium"), style.get("position", "mid_low"))
        points = _static_subtitles(ass)
        fonts_arg = str(fonts.resolve()).replace("\\", "/").replace(":", "\\:")
        subtitle_filter = f"subtitles=filename={ass.name}:fontsdir={fonts_arg}"
        exe = media.ffmpeg_with_subtitles()

    # Scenes with no time (a silent preview card) are skipped. Every scene is drawn in its own small process.
    jobs, start = [], 0
    for index, (source, count) in enumerate(zip(sources, frames)):
        if count > 0:
            jobs.append((source, start, count, segments_dir / f"{index:04d}.mp4"))
        start += count
    done = 0
    report(0, "Готовлю сцены…")
    with ThreadPoolExecutor(max_workers=max(1, min(parallel, 6))) as pool:
        futures = [pool.submit(_scene, source, first, count, fps, points, subtitle_filter, exe, work, seg)
                   for source, first, count, seg in jobs]
        try:
            for future in as_completed(futures):
                future.result()
                if stop():
                    raise Cancelled()
                done += 1
                report(done / max(len(jobs), 1) * .9, f"Сцены с субтитрами: {done} из {len(jobs)}")
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    listing = work / "scenes.txt"
    listing.write_text("".join(f"file '{seg.resolve()}'\nduration {count / fps:.6f}\n" for _, _, count, seg in jobs), "utf-8")
    report(.92, "Жду звук…")
    while sound_thread.is_alive():
        if stop():
            raise Cancelled()
        sound_thread.join(.5)
    if sound_error:
        raise sound_error[0]
    report(.96, "Склеиваю сцены и звук…")
    tmp = out.with_name(f".{out.stem}.fast.tmp.mp4")
    media.run(["-f", "concat", "-safe", 0, "-i", listing.resolve(), "-i", audio.resolve(), "-map", "0:v", "-map", "1:a",
               "-c", "copy", "-t", f"{total:.3f}", "-movflags", "+faststart", tmp.resolve()], "Склейка быстрой сборки")
    tmp.replace(out)
    return out


def _run_final(args: list, exe: str | None, cwd: Path, total: float, progress, stop):
    """A long FFmpeg pass that can be stopped: a pause or stop kills it instead of waiting."""
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
