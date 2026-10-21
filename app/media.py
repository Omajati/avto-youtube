"""FFmpeg helpers: audio, Ken Burns clips, final assembly."""
import os
import random
import re
import shutil
import subprocess
import wave
from pathlib import Path

from .net import UserError

_FFMPEG = None


def ffmpeg() -> str:
    global _FFMPEG
    if _FFMPEG:
        return _FFMPEG
    path = shutil.which("ffmpeg")
    if not path:
        try:
            import imageio_ffmpeg
            path = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as e:
            raise UserError(f"Не найден FFmpeg ({e}). Запустите установку ещё раз.")
    _FFMPEG = path
    return path


_SUBS_FFMPEG = None


def ffmpeg_with_subtitles() -> str:
    """An FFmpeg that can burn subtitles (needs libass). The bundled one can; a system one may not."""
    global _SUBS_FFMPEG
    if _SUBS_FFMPEG:
        return _SUBS_FFMPEG
    candidates = [ffmpeg()]
    try:
        import imageio_ffmpeg
        candidates.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        pass
    for exe in candidates:
        try:
            out = subprocess.run([exe, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        except OSError:
            continue
        if " subtitles " in out:
            _SUBS_FFMPEG = exe
            return exe
    raise UserError("Эта версия FFmpeg не умеет накладывать субтитры. Запустите установку программы ещё раз.")


def run(args: list, what: str = "FFmpeg", cwd=None, exe=None):
    p = subprocess.run([exe or ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *map(str, args)],
                       capture_output=True, text=True, cwd=cwd)
    if p.returncode != 0:
        raise UserError(f"{what}: ошибка сборки. {p.stderr[-600:]}")


def to_png(raw: bytes, out: Path):
    tmp = out.with_suffix(".raw")
    tmp.write_bytes(raw)
    try:
        run(["-i", tmp, "-frames:v", "1", out.with_suffix(".tmp.png")], "Картинка")
        out.with_suffix(".tmp.png").replace(out)
    finally:
        tmp.unlink(missing_ok=True)


def to_wav(src: Path, out: Path):
    tmp = out.with_suffix(".tmp.wav")
    run(["-i", src, "-ar", 44100, "-ac", 1, "-c:a", "pcm_s16le", tmp], "Звук")
    tmp.replace(out)


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def concat_wavs(parts: list, out: Path) -> list:
    """parts: [(wav_path, gap_seconds_after)]. Returns duration of each part including its gap."""
    durations = []
    with wave.open(str(out), "wb") as dst:
        dst.setnchannels(1)
        dst.setsampwidth(2)
        dst.setframerate(44100)
        for path, gap in parts:
            with wave.open(str(path), "rb") as src:
                frames = src.readframes(src.getnframes())
                n = src.getnframes()
            silence = int(44100 * gap)
            dst.writeframes(frames + b"\x00\x00" * silence)
            durations.append((n + silence) / 44100.0)
    return durations


MOTIONS = ["zoom_in", "pan_right", "zoom_out", "pan_left"]


def _shake(width: int, height: int) -> str:
    """A short camera shake that dies down in about a second (crop jitter, then back to full size)."""
    m = max(width, height) // 40
    return (f",crop=w=iw-{2 * m}:h=ih-{2 * m}:x='{m}+{m}*sin(t*47)*exp(-3*t)':y='{m}+{m}*cos(t*39)*exp(-3*t)',"
            f"scale={width}:{height}")


def render_clip(img: Path, out: Path, frames: int, width: int, height: int, fps: int,
                motion: str, strength: float, fade_in: bool = False, fade_out: bool = False, effect: str = "none"):
    n = max(frames, 1)
    s = float(strength)
    p = f"(on/{n})"
    hit = max(int(fps * 0.35), 1)  # frames of a fast punch-in
    if effect == "punch_in":
        z, x, y = f"if(lt(on,{hit}),1+0.22*on/{hit},1.22+0.04*(on-{hit})/{n})", "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"
    elif effect == "zoom_out":
        rev = max(int(fps * 0.9), 1)
        z, x, y = f"if(lt(on,{rev}),1.4-0.32*on/{rev},1.08-0.06*(on-{rev})/{n})", "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"
    elif motion == "zoom_out":
        z, x, y = f"{1 + s}-{s}*{p}", "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"
    elif motion == "pan_right":
        z, x, y = f"{1 + s}", f"(iw-iw/zoom)*{p}", "(ih-ih/zoom)/2"
    elif motion == "pan_left":
        z, x, y = f"{1 + s}", f"(iw-iw/zoom)*(1-{p})", "(ih-ih/zoom)/2"
    else:  # zoom_in
        z, x, y = f"1+{s}*{p}", "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"
    big_w, big_h = width * 2, height * 2  # upscaling keeps the slow motion smooth
    vf = (f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase,crop={big_w}:{big_h},setsar=1,"
          f"zoompan=z='{z}':x='{x}':y='{y}':d={n}:s={width}x{height}:fps={fps}")
    if effect == "shake":
        vf += _shake(width, height)
    vf += ",format=yuv420p"
    if fade_in:
        vf += f",fade=t=in:st=0:d=0.6"
    if fade_out:
        vf += f",fade=t=out:st={max(n / fps - 1.0, 0):.3f}:d=1.0"
    tmp = out.with_suffix(".tmp.mp4")
    run(["-i", img, "-vf", vf, "-frames:v", n, "-r", fps, "-c:v", "libx264", "-preset", "veryfast",
         "-crf", 20, "-pix_fmt", "yuv420p", tmp], "Видео-фрагмент")
    tmp.replace(out)


def video_duration(path: Path) -> float:
    p = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", p.stderr)
    if not m:
        raise UserError(f"Не удалось прочитать видео {path.name}")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def render_anim_clip(src: Path, out: Path, frames: int, width: int, height: int, fps: int,
                     fade_in: bool = False, fade_out: bool = False, effect: str = "none"):
    """Fits an animated clip to the scene length: slows it down a little if needed, then holds the last frame."""
    n = max(frames, 1)
    need = n / fps
    have = max(video_duration(src), 0.5)
    factor = min(max(need / have, 1.0), 1.9)
    vf = (f"setpts={factor:.4f}*PTS,fps={fps},scale={width}:{height}:force_original_aspect_ratio=increase,"
          f"crop={width}:{height},setsar=1,tpad=stop_mode=clone:stop_duration={need:.3f}"
          + (_shake(width, height) if effect == "shake" else "") + ",format=yuv420p")
    if fade_in:
        vf += ",fade=t=in:st=0:d=0.6"
    if fade_out:
        vf += f",fade=t=out:st={max(need - 1.0, 0):.3f}:d=1.0"
    tmp = out.with_suffix(".tmp.mp4")
    run(["-i", src, "-an", "-vf", vf, "-frames:v", n, "-r", fps, "-c:v", "libx264", "-preset", "veryfast",
         "-crf", 20, "-pix_fmt", "yuv420p", tmp], "Анимированный фрагмент")
    tmp.replace(out)


def concat_clips(clips: list, out: Path):
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{Path(c).resolve()}'\n" for c in clips), "utf-8")
    run(["-f", "concat", "-safe", 0, "-i", lst, "-c", "copy", out], "Склейка")
    lst.unlink(missing_ok=True)


def pick_music(music_dir: Path):
    files = [f for f in music_dir.iterdir() if f.suffix.lower() in (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")]
    return random.choice(files) if files else None


def final_mix(video: Path, narration: Path, music, out: Path, music_volume: float, lufs: float, total: float,
              subs: Path = None, fonts: Path = None, sfx: Path = None):
    """subs: an .ass file to burn into the picture (re-encodes the video), otherwise the video is copied."""
    args = ["-i", video, "-i", narration]
    if music:
        args += ["-stream_loop", -1, "-i", music]
        fade_st = max(total - 3, 0)
        fc = (f"[1:a]aformat=sample_rates=44100:channel_layouts=stereo[v];"
              f"[2:a]aformat=sample_rates=44100:channel_layouts=stereo,volume={music_volume},"
              f"afade=t=in:d=2,afade=t=out:st={fade_st:.2f}:d=3[m];"
              f"[v][m]amix=inputs=2:duration=first:normalize=0,loudnorm=I={lufs}:TP=-1.5:LRA=11[a]")
    else:
        fc = f"[1:a]aformat=sample_rates=44100:channel_layouts=stereo,loudnorm=I={lufs}:TP=-1.5:LRA=11[a]"
    if sfx:
        # Sound effects are added to the voice before the music, so loudness evens them out together.
        sidx = 3 if music else 2
        args += ["-i", sfx]
        fc = (f"[1:a][{sidx}:a]amix=inputs=2:duration=first:normalize=0[vs];"
              + fc.replace("[1:a]", "[vs]", 1))
    tmp = out.with_suffix(".tmp.mp4")
    cwd, exe = None, None
    if subs:
        # Run from the subtitles' folder so paths inside the filter need no escaping.
        cwd, exe = subs.parent, ffmpeg_with_subtitles()
        args = [str(Path(a).resolve()) if isinstance(a, Path) else a for a in args]
        tmp = tmp.resolve()
        rel_fonts = os.path.relpath(fonts, cwd).replace("\\", "/") if fonts else "."
        fc += f";[0:v]subtitles=filename={subs.name}:fontsdir={rel_fonts}[vo]"
        vmap, vcodec = "[vo]", ["-c:v", "libx264", "-preset", "veryfast", "-crf", 20, "-pix_fmt", "yuv420p"]
    else:
        vmap, vcodec = "0:v", ["-c:v", "copy"]
    run([*args, "-filter_complex", fc, "-map", vmap, "-map", "[a]", *vcodec, "-c:a", "aac",
         "-b:a", "192k", "-ar", 44100, "-t", f"{total:.3f}", "-movflags", "+faststart", tmp], "Финальная сборка",
        cwd=cwd, exe=exe)
    tmp.replace(out)


def thumbnail(img: Path, out: Path, vertical: bool = False):
    w, h = (1080, 1920) if vertical else (1280, 720)
    run(["-i", img, "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}", out], "Обложка")


# ---- Demo mode (checks the installation without any API keys) ----

def demo_image(out: Path, idx: int, vertical: bool):
    w, h = (768, 1344) if vertical else (1344, 768)
    hue = (idx * 47) % 360
    run(["-f", "lavfi", "-i", f"testsrc2=s={w}x{h}", "-frames:v", 1,
         "-vf", f"hue=h={hue}:s=0.6,boxblur=2", out], "Демо-картинка")


def demo_audio(out: Path, seconds: float, idx: int):
    freq = 220 + (idx % 5) * 55
    run(["-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds:.2f}", "-af", "volume=0.25",
         "-ar", 44100, "-ac", 1, "-c:a", "libmp3lame", "-b:a", "128k", out], "Демо-звук")
