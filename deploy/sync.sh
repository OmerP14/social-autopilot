#!/usr/bin/env bash
# Bilgisayardan sunucuya kod ve yeni fotoğrafları gönderir, servisi yeniden başlatır.
# Veritabanı, .env, fotoğraf kataloğu ve üretilen içerikler sunucuda kalır (üzerine yazılmaz).
# Telegram'dan gelen fotoğraflar sadece sunucudadır; bu betik sunucuda dosya silmez.
# Ayarlar: deploy/deploy.env (örnek: deploy/deploy.env.example) ya da ortam değişkenleri.
#   ./deploy/sync.sh
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f deploy/deploy.env ] && . deploy/deploy.env
: "${HOST:?HOST gerekli (deploy/deploy.env.example dosyasına bak)}"
KEY=${KEY:-~/.ssh/id_ed25519}
APP_DIR=${APP_DIR:-/opt/social-autopilot}
APP_USER=${APP_USER:-autopilot}
SERVICE=${SERVICE:-social-autopilot}
# Sunucuda üretim sürüyorsa bitmesini bekle (yeniden başlatma işi yarıda keser); en fazla 30 dk
for i in $(seq 1 180); do
  busy=$(ssh -i "$KEY" "$HOST" "find $APP_DIR/data/busy -type f -mmin -60 2>/dev/null | wc -l")
  [ "$busy" = "0" ] && break
  [ "$i" = "1" ] && echo "Sunucuda üretim sürüyor, bitmesi bekleniyor…"
  sleep 10
done
if command -v rsync >/dev/null; then
  rsync -az -e "ssh -i $KEY" \
    --exclude .venv --exclude __pycache__ --exclude .ruff_cache --exclude .git --exclude .DS_Store \
    --exclude .env --exclude data/ --exclude output/ --exclude backups/ --exclude panel/ \
    --exclude deploy/deploy.env --exclude '*.local.md' \
    --exclude 'clients/*/photos/catalog.json' --exclude 'clients/*/music/' \
    --exclude 'clients/*/design_new/' --exclude 'clients/*/design_prev/' --exclude 'clients/*/design_options/' \
    ./ "$HOST:$APP_DIR/"
else   # Windows (Git Bash): rsync yok → aynı dışlamalarla tar üzerinden gönder (sunucuda dosya silmez)
  tar -cf - --exclude=./.venv --exclude=__pycache__ --exclude=./.ruff_cache --exclude=./.pytest_cache \
    --exclude=./.git --exclude=.DS_Store \
    --exclude=./.env --exclude=./data --exclude=./output --exclude=./backups --exclude=./panel \
    --exclude=./deploy/deploy.env --exclude='*.local.md' \
    --exclude='./clients/*/photos/catalog.json' --exclude='./clients/*/music' \
    --exclude='./clients/*/design_new' --exclude='./clients/*/design_prev' --exclude='./clients/*/design_options' . \
    | ssh -i "$KEY" "$HOST" "tar -xf - -C $APP_DIR --no-same-owner"
fi
ssh -i "$KEY" "$HOST" "chown -R $APP_USER:$APP_USER $APP_DIR && cd $APP_DIR && sudo -u $APP_USER /home/$APP_USER/.local/bin/uv sync --frozen -q && systemctl restart $SERVICE && systemctl is-active $SERVICE"
