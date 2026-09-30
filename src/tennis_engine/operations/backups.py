"""F15.7 backups for the agreed recovery objectives (RTO 4 h, RPO 15 min, 2026-09-30).

Three stores are backed up:

- PostgreSQL: continuous WAL archiving (`archive_timeout` 60 s) and a daily base backup
  (`deploy/postgres/`). Worst-case loss: about one archive timeout.
- Raw objects: `backup_objects` copies every new key to the backup volume every 5 min.
  Worst-case loss: one object backup interval.
- F01 journal: `snapshot_journal` takes a SQLite online backup every 5 min. Worst-case
  loss: one journal backup interval.

`RecoveryConfig.design_findings` checks the schedule against the RPO. The restore drill
(`scripts/ops02_restore_drill.py`) measures the real restore time and data-loss window.
The backup files live on a dedicated backup volume on the same host. A copy off the host
is not configured (no second host exists), so a host loss is outside these objectives.
"""

from collections.abc import Callable, Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field
from sqlalchemy import Engine, text

from tennis_engine.common.clock import require_aware
from tennis_engine.common.contracts import Contract, Identifier
from tennis_engine.infrastructure.object_store import ImmutableObjectStore, LocalObjectStore
from tennis_engine.monitoring.metrics import Family, MetricsRegistry, Sample

from .recovery import Objectives, backup_journal

DEFAULT_RECOVERY_CONFIG = Path("configs/operations/recovery-objectives.json")
Seconds = Annotated[int, Field(gt=0, le=7 * 86_400, strict=True)]
OBJECT_MARKER = ".last-backup"
JOURNAL_PREFIX = "governance-"


class RecoverySchedule(Contract):
    wal_archive_timeout_seconds: Seconds
    base_backup_interval_seconds: Seconds
    object_backup_interval_seconds: Seconds
    journal_backup_interval_seconds: Seconds


class RecoveryConfig(Contract):
    version: Identifier
    status: Literal["ACCEPTED"]
    accepted_on: date
    accepted_by: Annotated[str, Field(min_length=1, max_length=64)]
    note: Annotated[str, Field(min_length=1, max_length=500)]
    rto_seconds: Seconds
    rpo_seconds: Seconds
    schedule: RecoverySchedule
    retention: dict[str, int | str]

    def objectives(self) -> Objectives:
        return Objectives(rto_seconds=self.rto_seconds, rpo_seconds=self.rpo_seconds)

    def worst_case_loss_seconds(self) -> dict[str, int]:
        """The longest possible data-loss window of each store under the schedule."""
        return {
            "postgres": self.schedule.wal_archive_timeout_seconds,
            "objects": self.schedule.object_backup_interval_seconds,
            "journal": self.schedule.journal_backup_interval_seconds,
        }

    def design_findings(self) -> tuple[str, ...]:
        return tuple(
            f"SCHEDULE_EXCEEDS_RPO:{store}"
            for store, seconds in self.worst_case_loss_seconds().items()
            if seconds > self.rpo_seconds
        )


def load_recovery_config(path: Path = DEFAULT_RECOVERY_CONFIG) -> RecoveryConfig:
    return RecoveryConfig.model_validate_json(path.read_bytes())


class CopyReport(Contract):
    listed: int
    copied: int
    problems: tuple[str, ...]


def copy_missing(source: ImmutableObjectStore, target: ImmutableObjectStore) -> CopyReport:
    """Copy each key of `source` that `target` does not have. Existing keys are kept.

    Objects are immutable, so an existing key needs no copy. `put` refuses a key with
    other bytes, which is reported as a problem. Nothing is deleted.
    """
    keys = source.list_keys()
    copied = 0
    problems: list[str] = []
    for key in keys:
        try:
            if target.exists(key):
                continue
            target.put(key, source.get(key))
            copied += 1
        except FileExistsError:
            problems.append("OBJECT_CONFLICT")
        except Exception as error:  # noqa: BLE001 - every failure is reported
            problems.append(f"OBJECT_COPY_FAILED:{type(error).__name__}")
    return CopyReport(listed=len(keys), copied=copied, problems=tuple(problems))


def backup_objects(source: ImmutableObjectStore, directory: Path, now: datetime) -> CopyReport:
    """Copy new raw objects to the backup volume, then record the recovery point."""
    report = copy_missing(source, LocalObjectStore(directory))
    if not report.problems:
        marker = directory / OBJECT_MARKER
        temporary = directory / f"{OBJECT_MARKER}.tmp"
        temporary.write_text(str(int(require_aware(now).timestamp())), encoding="utf-8")
        temporary.replace(marker)
    return report


def snapshot_journal(journal: Path, directory: Path, window: datetime, keep: int) -> Path:
    """One consistent journal copy per window. Keep the newest `keep` copies."""
    if keep < 1:
        raise ValueError("Keep at least one journal snapshot")
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{JOURNAL_PREFIX}{require_aware(window):%Y%m%dT%H%M%SZ}.sqlite3"
    target = directory / name
    if not target.exists():
        temporary = directory / f".{name}.tmp"
        temporary.unlink(missing_ok=True)
        backup_journal(journal, temporary)
        temporary.replace(target)
    snapshots = journal_snapshots(directory)
    for old in snapshots[:-keep]:
        old.unlink(missing_ok=True)
    return target


def journal_snapshots(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(directory.glob(f"{JOURNAL_PREFIX}*.sqlite3"))


def _snapshot_time(path: Path) -> float | None:
    stamp = path.name.removeprefix(JOURNAL_PREFIX).removesuffix(".sqlite3")
    try:
        return datetime.strptime(stamp, "%Y%m%dT%H%M%S%z").timestamp()
    except ValueError:
        return None


def newest_base_backup(directory: Path) -> float | None:
    """Unix time of the newest complete PostgreSQL base backup, or None."""
    newest = None
    for marker in directory.glob("*/COMPLETE") if directory.is_dir() else ():
        try:
            value = float(marker.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        newest = value if newest is None else max(newest, value)
    return newest


BACKUP_SUCCESS = Family(
    "tennis_backup_last_success_timestamp_seconds",
    "gauge",
    "Unix time of the newest recovery point of each store. Absent when there is none.",
    ("store",),
)
WAL_ARCHIVE = Family(
    "tennis_wal_archive_timestamp_seconds",
    "gauge",
    "Unix time of the last archived and the last failed WAL segment (pg_stat_archiver).",
    ("event",),
)
WAL_COUNT = Family(
    "tennis_wal_archive_segments",
    "gauge",
    "Archived and failed WAL segments since the statistics reset (pg_stat_archiver).",
    ("event",),
)


def wal_archiver(engine: Engine) -> dict[str, float | None]:
    with engine.connect() as db:
        row = db.execute(
            text(
                "SELECT archived_count, failed_count, "
                "extract(epoch FROM last_archived_time) AS archived_at, "
                "extract(epoch FROM last_failed_time) AS failed_at FROM pg_stat_archiver"
            )
        ).one()
    return {
        "archived_count": float(row.archived_count),
        "failed_count": float(row.failed_count),
        "archived_at": None if row.archived_at is None else float(row.archived_at),
        "failed_at": None if row.failed_at is None else float(row.failed_at),
    }


def register_backup_collector(
    registry: MetricsRegistry,
    *,
    postgres_base: Path | None,
    objects: Path | None,
    journal: Path | None,
    archiver: Callable[[], dict[str, float | None]] | None,
) -> None:
    """Recovery points read from the backup volumes at scrape time, so a restart of the
    scheduler does not hide or invent a backup."""

    def collect() -> Iterable[Sample]:
        samples: list[Sample] = []
        points: dict[str, float | None] = {}
        if postgres_base is not None:
            points["postgres_base"] = newest_base_backup(postgres_base)
        if objects is not None:
            marker = objects / OBJECT_MARKER
            points["objects"] = (
                float(marker.read_text(encoding="utf-8")) if marker.is_file() else None
            )
        if journal is not None:
            snapshots = journal_snapshots(journal)
            points["journal"] = _snapshot_time(snapshots[-1]) if snapshots else None
        for store, value in sorted(points.items()):
            if value is not None:
                samples.append(Sample(BACKUP_SUCCESS.name, value, (("store", store),)))
        if archiver is not None:
            state = archiver()
            for event in ("archived", "failed"):
                count = state[f"{event}_count"]
                if count is not None:
                    samples.append(Sample(WAL_COUNT.name, count, (("event", event),)))
                at = state[f"{event}_at"]
                if at is not None:
                    samples.append(Sample(WAL_ARCHIVE.name, at, (("event", event),)))
        return samples

    registry.register_collector("backups", collect, (BACKUP_SUCCESS, WAL_ARCHIVE, WAL_COUNT))
