#!/bin/bash
# Ставит или обновляет программу в ~/AutoYouTube. Запускается из «Авто ютуб.app»; папку data не трогает.
set -e
SRC="$1"
DEST="$HOME/AutoYouTube"
mkdir -p "$DEST/data"

# Старая копия программы, запущенная раньше, останавливается, чтобы открылась новая.
curl -s -X POST --max-time 3 http://127.0.0.1:8765/api/shutdown >/dev/null 2>&1 && sleep 2 || true

# 1. Файлы программы
rm -rf "$DEST/app" "$DEST/prompts_default"
cp -R "$SRC/app" "$SRC/prompts_default" "$DEST/"
cp "$SRC/run.py" "$SRC/requirements.txt" "$DEST/"
for f in README.md "О программе Авто ютуб.txt"; do [ -f "$SRC/$f" ] && cp "$SRC/$f" "$DEST/"; done
xattr -dr com.apple.quarantine "$DEST/app" 2>/dev/null || true
echo "✓ файлы программы"

# 2. uv ставит Python и библиотеки, не трогая систему
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
if [ ! -x "$DEST/.venv/bin/python" ]; then
  uv venv --python 3.12 "$DEST/.venv"
fi
uv pip install --python "$DEST/.venv/bin/python" -q -r "$DEST/requirements.txt"
"$DEST/.venv/bin/python" -c "import imageio_ffmpeg; imageio_ffmpeg.get_ffmpeg_exe()" >/dev/null
echo "✓ Python, библиотеки и FFmpeg"

# 3. Резервная копия и папка на рабочем столе
BK="$HOME/Desktop/Авто ютуб — резерв"
mkdir -p "$BK"
(cd "$DEST" && ./.venv/bin/python -m app.backup >/dev/null 2>&1) || true
[ -f "$SRC/О программе Авто ютуб.txt" ] && cp "$SRC/О программе Авто ютуб.txt" "$BK/"
rm -f "$BK/программа (последняя версия).zip"
APPDIR="$(cd "$SRC/../../.." && pwd)"
case "$APPDIR" in *.app) ditto -c -k --keepParent "$APPDIR" "$BK/Авто ютуб (программа).zip" 2>/dev/null || true ;; esac
cat > "$BK/Сделать резервную копию.command" <<'CMD'
#!/bin/bash
cd "$HOME/AutoYouTube" && ./.venv/bin/python -m app.backup
echo ""; echo "Окно можно закрыть."
CMD
cat > "$BK/Восстановить из копии.command" <<'CMD'
#!/bin/bash
cd "$HOME/AutoYouTube" || { echo "Сначала запустите программу «Авто ютуб» один раз."; exit 1; }
echo "=== Восстановление «Авто ютуб» из резервной копии ==="
echo "Перед восстановлением выключите программу (кнопка «Выключить программу» слева внизу)."
echo ""
./.venv/bin/python -m app.backup list
echo ""
read -p "Номер копии для восстановления (Enter — отмена): " N
[ -z "$N" ] && { echo "Отменено."; exit 0; }
./.venv/bin/python -m app.backup restore "$N"
echo ""; echo "Готово. Запустите «Авто ютуб» из Программ. Окно можно закрыть."
CMD
chmod +x "$BK/"*.command
xattr -dr com.apple.quarantine "$BK" 2>/dev/null || true
echo "✓ резервная копия"

# 4. Старые значки-ярлыки (открывали программу в браузере) больше не нужны
for OLDAPP in "$HOME/Desktop/Авто ютуб.app" "$HOME/Applications/Авто ютуб.app"; do
  if grep -q "open http://127.0.0.1:8765" "$OLDAPP/Contents/MacOS/launch" 2>/dev/null; then
    rm -rf "$OLDAPP"
  fi
done
rm -f "$HOME/Desktop/Авто ютуб.command"
echo "✓ старые ярлыки убраны"

cp "$SRC/VERSION" "$DEST/VERSION"
echo "✓ готово"
