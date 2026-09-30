#!/bin/sh
# F15.7 scheduled PostgreSQL base backups (RPO 15 min, RTO 4 h; user decision 2026-09-30).
#
#   sh base-backup.sh          loop: take a base backup when the newest one is too old
#   sh base-backup.sh --once   take one base backup, then exit
#
# The WAL archive (archive-wal.sh) holds every change after a base backup, so a restore
# replays the archive on top of the newest base backup. Layout under BACKUP_ROOT:
#
#   base/<UTC time>/base.tar.gz, backup_manifest   the backup (pg_basebackup -Ft -z)
#   base/<UTC time>/START_WAL                      a WAL file at or before its start
#   base/<UTC time>/COMPLETE                       Unix time when the backup finished
#   wal/                                           the WAL archive
#
# Retention: keep BASE_BACKUP_KEEP complete backups (at least 2). Remove WAL files older
# than the START_WAL of the oldest kept backup. An incomplete backup (*.part) is removed.
# The script prints no password. PGPASSWORD_FILE names a secret file.
set -eu

BACKUP_ROOT="${BACKUP_ROOT:-/backup}"
INTERVAL="${BASE_BACKUP_INTERVAL_SECONDS:-86400}"
KEEP="${BASE_BACKUP_KEEP:-7}"
CHECK_SECONDS="${BASE_BACKUP_CHECK_SECONDS:-60}"
if [ "$KEEP" -lt 2 ]; then KEEP=2; fi
if [ -n "${PGPASSWORD_FILE:-}" ]; then
  PGPASSWORD="$(cat "$PGPASSWORD_FILE")"
  export PGPASSWORD
fi

log() {
  printf '{"level":"%s","logger":"postgres-backup","message":"%s"}\n' "$1" "$2"
}

newest_complete() {
  newest=0
  for marker in "$BACKUP_ROOT"/base/*/COMPLETE; do
    [ -f "$marker" ] || continue
    value="$(cat "$marker")"
    if [ "$value" -gt "$newest" ]; then newest="$value"; fi
  done
  echo "$newest"
}

prune() {
  rm -rf "$BACKUP_ROOT"/base/*.part
  kept="$(ls -1d "$BACKUP_ROOT"/base/*/COMPLETE 2>/dev/null | sort | wc -l)"
  if [ "$kept" -gt "$KEEP" ]; then
    remove=$((kept - KEEP))
    ls -1d "$BACKUP_ROOT"/base/*/COMPLETE | sort | head -n "$remove" | while read -r marker; do
      rm -rf "$(dirname "$marker")"
    done
  fi
  oldest="$(ls -1d "$BACKUP_ROOT"/base/*/COMPLETE 2>/dev/null | sort | head -n 1)"
  if [ -n "$oldest" ] && [ -f "$(dirname "$oldest")/START_WAL" ]; then
    pg_archivecleanup "$BACKUP_ROOT/wal" "$(cat "$(dirname "$oldest")/START_WAL")"
  fi
}

# `set -e` does not apply inside a function that runs in an `||` list, so every step
# checks its own result. A failed step leaves only a *.part directory, never COMPLETE.
take() {
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  target="$BACKUP_ROOT/base/$stamp"
  rm -rf "$target.part"
  # A WAL file at or before the backup start. Older WAL is not needed for this backup.
  start_wal="$(psql -X -At -c "SELECT pg_walfile_name(pg_current_wal_lsn())")" || return 1
  [ -n "$start_wal" ] || return 1
  pg_basebackup -D "$target.part" -Ft -z -X none -c fast --no-password || return 1
  [ -s "$target.part/base.tar.gz" ] || return 1
  echo "$start_wal" > "$target.part/START_WAL" || return 1
  date -u +%s > "$target.part/COMPLETE" || return 1
  # The scheduler (group of the setgid base directory) reads the markers, not the data.
  chmod 0750 "$target.part" || return 1
  chmod 0640 "$target.part/COMPLETE" "$target.part/START_WAL" || return 1
  mv "$target.part" "$target" || return 1
  log INFO "base backup complete: $stamp"
  prune || log ERROR "retention cleanup failed"
}

mkdir -p "$BACKUP_ROOT/base" "$BACKUP_ROOT/wal"
if [ "${1:-}" = "--once" ]; then
  if take; then exit 0; fi
  log ERROR "base backup failed"
  exit 1
fi
while true; do
  now="$(date -u +%s)"
  if [ $((now - $(newest_complete))) -ge "$INTERVAL" ]; then
    take || log ERROR "base backup failed"
  fi
  sleep "$CHECK_SECONDS"
done
