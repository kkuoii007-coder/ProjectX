#!/bin/sh
# Daily logical backup of the philosophy_reader database.
# Intended to run as the postgres OS user (peer auth, no password needed):
#   sudo -u postgres /usr/local/sbin/backup-philosophy-reader
# Dump format is custom (compressed); restore:
#   pg_restore -c -U philosophy_reader -d philosophy_reader <file>
set -eu

# Avoid inheriting a cwd the postgres user cannot re-enter after find -delete.
cd /

BACKUP_DIR=/var/backups/philosophy-reader
KEEP_DAYS=7

umask 077
mkdir -p "$BACKUP_DIR"

STAMP=$(date +%Y%m%d-%H%M%S)
TMP="$BACKUP_DIR/db-$STAMP.dump.tmp"
OUT="$BACKUP_DIR/db-$STAMP.dump"

pg_dump --format=custom --file="$TMP" philosophy_reader
mv "$TMP" "$OUT"

# Retention: delete dumps older than KEEP_DAYS.
find "$BACKUP_DIR" -name 'db-*.dump' -mtime +$KEEP_DAYS -delete
