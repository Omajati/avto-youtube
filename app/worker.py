"""Background workers: several videos are made at once (setting jobs_parallel); the assembly goes one at a time."""
import shutil
import subprocess
import threading
import time
import traceback

from . import db, pipeline, uploader
from .net import Cancelled, UserError

_wake = threading.Event()
_lock = threading.Lock()
_running = set()  # ids of the jobs the workers are making right now
MAX_WORKERS = 5
_pending = {}     # job id -> stage to redo once the current run has stopped


def wake():
    _wake.set()


def reset_when_stopped(jid: str, stage: str) -> bool:
    """If the job is being made right now, remember to redo it from `stage` as soon as the run stops.
    Deleting its files earlier would let the old run write stale results or crash. Returns False if it isn't running."""
    with _lock:
        if jid not in _running:
            return False
        _pending[jid] = stage
    db.update(jid, status="paused")
    return True


def _keep_awake():
    """macOS: don't let the Mac fall asleep while a video is being made."""
    if shutil.which("caffeinate"):
        try:
            return subprocess.Popen(["caffeinate", "-i"])
        except OSError:
            return None
    return None


def _limit() -> int:
    from .config import load_settings
    try:
        return max(1, min(MAX_WORKERS, int(load_settings().get("jobs_parallel", 3))))
    except (TypeError, ValueError):
        return 3


def _claim(slot: int):
    """The next job nobody is making yet, or None. Slot k works only while fewer than the limit run."""
    with _lock:
        if slot >= _limit() or len(_running) >= _limit():
            return None
        job = next((j for j in db.next_jobs(20) if j["id"] not in _running), None)
        if job:
            _running.add(job["id"])
        return job


def loop(slot: int = 0):
    while True:
        job = _claim(slot)
        if not job:
            _wake.wait(5)
            if slot == 0:
                _wake.clear()
            continue
        jid = job["id"]
        db.update(jid, status="running", error="")
        awake = _keep_awake()
        c_log = pipeline.job_dir(jid) / "log.txt"
        try:
            pipeline.run_job(job)
            if db.get(jid):
                db.update(jid, status="done")
                uploader.auto(jid)
        except pipeline.StopForReview as e:
            db.update(jid, status="review")
            _append(c_log, str(e) or "⏸ Сценарий готов и ждёт вашей проверки. Нажмите «Продолжить», чтобы озвучить и собрать видео.")
        except Cancelled:
            pass  # paused or deleted by the user
        except UserError as e:
            if db.get(jid):
                db.update(jid, status="error", error=str(e))
            _append(c_log, f"✗ Ошибка: {e}")
        except Exception as e:  # unexpected bug
            if db.get(jid):
                db.update(jid, status="error", error=f"Непредвиденная ошибка: {e}")
            _append(c_log, "✗ Непредвиденная ошибка:\n" + traceback.format_exc())
        finally:
            if awake:
                awake.terminate()
            with _lock:
                _running.discard(jid)
                stage = _pending.pop(jid, None)
            _wake.set()  # a free place: another worker can take the next video
            if stage and db.get(jid):
                try:
                    pipeline.reset_from(jid, stage)
                    db.update(jid, status="queued", error="")
                except Exception:
                    _append(c_log, "✗ Не удалось начать заново:\n" + traceback.format_exc())


def _append(path, msg):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def start():
    for slot in range(MAX_WORKERS):
        threading.Thread(target=loop, args=(slot,), daemon=True).start()
