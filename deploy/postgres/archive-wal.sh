#!/bin/sh
# F15.7 WAL archive command: archive-wal.sh <path> <file name>
# PostgreSQL runs it for every completed WAL segment (archive_command).
# - A new segment is copied to a temporary name, flushed, then renamed. A reader never
#   sees a partial file.
# - A segment that is already archived with the same bytes is a success, so a retry after
#   a crash does not stop archiving.
# - A segment that is already archived with other bytes is a failure. PostgreSQL keeps the
#   segment and retries; the WAL archive alert fires.
set -eu
source_path="$1"
name="$2"
archive="${WAL_ARCHIVE_DIR:-/backup/wal}"
target="$archive/$name"
if [ -f "$target" ]; then
  cmp -s "$source_path" "$target"
  exit $?
fi
cp "$source_path" "$target.part"
sync "$target.part" 2>/dev/null || sync
mv "$target.part" "$target"
