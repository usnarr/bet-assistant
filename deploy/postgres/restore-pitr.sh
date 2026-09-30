#!/bin/sh
# F15.7 point-in-time restore into an empty data directory, then start PostgreSQL.
#
# Use it as the entrypoint of a PostgreSQL container that mounts the backup volume at
# BACKUP_ROOT and has an empty PGDATA. It extracts the newest complete base backup, or
# BASE_BACKUP=<UTC time> when set, then replays the whole WAL archive
# (restore_command) and promotes the server. RECOVERY_TARGET_TIME stops the replay earlier.
# A non-empty PGDATA is never changed.
set -eu

BACKUP_ROOT="${BACKUP_ROOT:-/backup}"
PGDATA="${PGDATA:-/var/lib/postgresql/data}"

if [ -n "$(ls -A "$PGDATA" 2>/dev/null)" ]; then
  echo '{"level":"ERROR","logger":"postgres-restore","message":"PGDATA is not empty"}'
  exit 1
fi
if [ -n "${BASE_BACKUP:-}" ]; then
  source_dir="$BACKUP_ROOT/base/$BASE_BACKUP"
else
  marker="$(ls -1d "$BACKUP_ROOT"/base/*/COMPLETE 2>/dev/null | sort | tail -n 1)"
  source_dir="$(dirname "$marker")"
fi
if [ ! -f "$source_dir/COMPLETE" ]; then
  echo '{"level":"ERROR","logger":"postgres-restore","message":"no complete base backup"}'
  exit 1
fi
chmod 0700 "$PGDATA"
tar -xzf "$source_dir/base.tar.gz" -C "$PGDATA"
touch "$PGDATA/recovery.signal"
{
  echo "restore_command = 'cp $BACKUP_ROOT/wal/%f %p'"
  echo "recovery_target_action = 'promote'"
  echo "recovery_target_timeline = 'latest'"
  if [ -n "${RECOVERY_TARGET_TIME:-}" ]; then
    echo "recovery_target_time = '$RECOVERY_TARGET_TIME'"
  fi
} >> "$PGDATA/postgresql.auto.conf"
echo '{"level":"INFO","logger":"postgres-restore","message":"base backup extracted; replaying the WAL archive"}'
exec postgres "$@"
