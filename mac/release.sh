#!/bin/bash
# Готовит выпуск новой версии в хранилище: собирает приложение и пишет release/latest.json.
# Запуск из корня хранилища: bash mac/release.sh "что нового"   (потом git commit и git push)
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NOTES="$1"
VER="$(grep -o 'class="ver">[^<]*' "$ROOT/app/static/index.html" | sed 's/.*(\([0-9]*\)).*/\1/')"
LABEL="$(grep -o 'class="ver">[^<]*' "$ROOT/app/static/index.html" | sed 's/.*): //')"
TMP="$(mktemp -d)"
bash "$ROOT/mac/build.sh" "$TMP" >/dev/null
mkdir -p "$ROOT/release"
cp "$TMP/Авто ютуб.zip" "$ROOT/release/Avto-YouTube.zip"
rm -rf "$TMP"
REPO="$(grep -o '^REPO = "[^"]*' "$ROOT/app/updater.py" | cut -d'"' -f2)"
${PY:-python3} -c "import json, sys; print(json.dumps({'version': int(sys.argv[1]), 'label': sys.argv[2], 'notes': sys.argv[3], 'app_zip': f'https://raw.githubusercontent.com/{sys.argv[4]}/main/release/Avto-YouTube.zip'}, ensure_ascii=False, indent=1))" "$VER" "$LABEL" "$NOTES" "$REPO" > "$ROOT/release/latest.json"
echo "Версия $VER: release/latest.json и release/Avto-YouTube.zip готовы"
