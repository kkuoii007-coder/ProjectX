#!/bin/sh
# Push the newest encrypted dump off-site via rclone.
# Requires: /etc/philosophy-reader/rclone.conf (remote configured by the owner)
# and /etc/philosophy-reader/backup-encryption.key (created by install-offsite.sh).
# Runs as root from cron AFTER the daily dump: 05 4 * * *.
set -eu

KEYFILE=/etc/philosophy-reader/backup-encryption.key
RCLONE_CONF=/etc/philosophy-reader/rclone.conf
BACKUP_DIR=/var/backups/philosophy-reader
LOG=/var/log/philosophy-reader-offsite.log

[ -f "$KEYFILE" ] || { echo "no encryption key" >&2; exit 1; }
[ -f "$RCLONE_CONF" ] || { echo "no rclone config; run: rclone config --config $RCLONE_CONF" >&2; exit 1; }

LATEST=$(ls -1t "$BACKUP_DIR"/db-*.dump 2>/dev/null | head -1) || true
[ -n "$LATEST" ] || { echo "no dump to push" >&2; exit 1; }

ENC="$LATEST.enc"
openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -salt -in "$LATEST" -out "$ENC" -pass file:"$KEYFILE"

REMOTE=$(awk '/^\[/{name=$0} /^type/{print name}' "$RCLONE_CONF" | head -1 | tr -d '[] ')
TARGET="philosophy-backup:philosophy-reader"
rclone copy "$ENC" "$TARGET/backups" --config "$RCLONE_CONF" --quiet
rm -f "$ENC"

echo "$(date -u +%FT%TZ) pushed $(basename "$ENC") to $TARGET" >> "$LOG"
