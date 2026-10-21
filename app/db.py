"""SQLite job queue. Survives restarts: unfinished jobs are resumed."""
import json
import sqlite3
import threading
import time
import uuid

from .config import DB_FILE, JOBS_DIR

_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(DB_FILE, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _lock, _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                created REAL, updated REAL,
                status TEXT, stage TEXT, progress REAL,
                title TEXT, topic TEXT,
                options TEXT, error TEXT,
                approved INTEGER DEFAULT 0,
                position REAL
            )"""
        )


# status: queued | running | review | paused | done | error
def create(topic: str, options: dict) -> dict:
    jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    now = time.time()
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO jobs (id, created, updated, status, stage, progress, title, topic, options, error, approved, position)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (jid, now, now, "queued", "", 0, "", topic, json.dumps(options, ensure_ascii=False), "", 0, now),
        )
    (JOBS_DIR / jid).mkdir(parents=True, exist_ok=True)
    return get(jid)


def _row(r):
    if r is None:
        return None
    d = dict(r)
    d["options"] = json.loads(d["options"] or "{}")
    return d


def get(jid: str):
    with _conn() as c:
        return _row(c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())


def list_all():
    with _conn() as c:
        return [_row(r) for r in c.execute("SELECT * FROM jobs ORDER BY created DESC").fetchall()]


def update(jid: str, **fields):
    if not fields:
        return
    if "options" in fields:
        fields["options"] = json.dumps(fields["options"], ensure_ascii=False)
    fields["updated"] = time.time()
    keys = ", ".join(f"{k}=?" for k in fields)
    with _lock, _conn() as c:
        c.execute(f"UPDATE jobs SET {keys} WHERE id=?", (*fields.values(), jid))


def delete(jid: str):
    with _lock, _conn() as c:
        c.execute("DELETE FROM jobs WHERE id=?", (jid,))


def next_job():
    """Oldest job that should be worked on. 'running' means it was interrupted (Mac turned off)."""
    with _conn() as c:
        r = c.execute(
            "SELECT * FROM jobs WHERE status IN ('running','queued') "
            "ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, position ASC LIMIT 1"
        ).fetchone()
        return _row(r)
