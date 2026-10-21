"""Backups of settings, projects, characters and prompts to a folder on the Desktop, and restoring from them.
Videos (data/jobs) are not included: they are big and can be made again.

Command line:  python -m app.backup            make a backup
               python -m app.backup list       numbered list of backups
               python -m app.backup restore N  restore backup number N from the list
"""
import io
import sys
import time
import zipfile
from pathlib import Path

from .config import DATA_DIR

BACKUP_DIR = Path.home() / "Desktop" / "Авто ютуб — резерв"
# If macOS doesn't let the program write to the Desktop, copies go next to the program instead.
FALLBACK_DIR = DATA_DIR.parent / "Авто ютуб — резерв"
KEEP = 15
INCLUDE = ["settings.json", "niche_templates.json", "projects", "characters", "items", "prompts", "youtube", "sfx", "music"]


def make(reason: str = "") -> Path:
    name = time.strftime("копия %Y-%m-%d %H-%M-%S") + (f" ({reason})" if reason else "") + ".zip"
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        out = BACKUP_DIR / name
        out.touch()
    except OSError:
        FALLBACK_DIR.mkdir(parents=True, exist_ok=True)
        out = FALLBACK_DIR / name
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for item in INCLUDE:
            p = DATA_DIR / item
            if p.is_file():
                z.write(p, item)
            elif p.is_dir():
                for f in p.rglob("*"):
                    if f.is_file() and ".cache" not in f.parts and f.suffix != ".tmp":
                        z.write(f, f.relative_to(DATA_DIR).as_posix())
    old = backups()[KEEP:]
    for f in old:
        f.unlink(missing_ok=True)
    return out


def backups() -> list:
    """Newest first."""
    found = []
    for d in (BACKUP_DIR, FALLBACK_DIR):
        try:
            found += list(d.glob("копия *.zip"))
        except OSError:
            continue
    return sorted(found, key=lambda f: f.stat().st_mtime, reverse=True)


def restore(zip_path: Path) -> Path:
    """Puts the backup's files back. The current state is saved as a backup first, so nothing is lost."""
    # Read the chosen copy before making the safety copy: making it may prune the oldest copy, which can be this one.
    data = io.BytesIO(Path(zip_path).read_bytes())
    safety = make("перед восстановлением")
    with zipfile.ZipFile(data) as z:
        for n in z.namelist():
            if n.startswith("/") or ".." in Path(n).parts:
                continue
            z.extract(n, DATA_DIR)
    return safety


def _main(args):
    if not args:
        print(f"✓ Резервная копия сохранена: {make()}")
        return
    items = backups()
    if args[0] == "list":
        if not items:
            print("Резервных копий пока нет.")
        for i, f in enumerate(items, 1):
            print(f"{i}. {f.stem}")
        return
    if args[0] == "restore" and len(args) > 1 and args[1].isdigit() and 0 < int(args[1]) <= len(items):
        src = items[int(args[1]) - 1]
        safety = restore(src)
        print(f"✓ Восстановлено из «{src.stem}».")
        print(f"  То, что было до восстановления, сохранено в «{safety.stem}».")
        return
    print("Не понял команду. Пример: python -m app.backup restore 1")


if __name__ == "__main__":
    _main(sys.argv[1:])
