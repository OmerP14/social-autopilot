#!/usr/bin/env bash
# Günlük SQLite yedeği (14 gün saklanır). cron: 0 4 * * * /opt/social-autopilot/deploy/backup.sh
set -euo pipefail
umask 077   # yedekler (token içerir) sadece sahibine okunur
APP_DIR=${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}
mkdir -p "$APP_DIR/backups"
sqlite3 "$APP_DIR/data/autopilot.db" ".backup '$APP_DIR/backups/autopilot-$(date +%F).db'"
find "$APP_DIR/backups" -name 'autopilot-*.db' -mtime +14 -delete
# Eski ayların ara dosyaları (HTML, kırpılmış fotoğraf) 30 günden eskiyse silinir
find "$APP_DIR/output" -path '*/_work/*' -type f -mtime +30 -delete 2>/dev/null || true
