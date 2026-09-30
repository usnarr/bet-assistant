"""OPS-02 model rollback drill on an isolated PostgreSQL database. Synthetic data only.

Steps:

1. Train two synthetic bundles (baseline, calibrator, card, evaluation report) and
   register them. The second names the first as its rollback target.
2. Show that the real F13 decision on the synthetic walk-forward run is not PASS, so the
   switch is refused. Nothing is promoted automatically.
3. Show that a mixed bundle, a self-approval and a switch by the author are refused.
4. With PASS decision fixtures (drill only, not model evidence), promote the first and
   then the second bundle.
5. Roll back and measure the time until the restored bundle loads with every hash checked.
6. Change one file of the rollback target: the rollback is refused. Restore the file from
   the backup copy: the rollback succeeds.

Usage (Git Bash; the database must be a disposable test database):

    TEST_DATABASE_URL=postgresql+psycopg://tennis:test-only@127.0.0.1:55444/tennis_test \\
      uv run python scripts/ops02_rollback_drill.py

The report prints IDs, codes and durations, never a local path. The script downgrades
the database to base at the end.
"""

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from registry_support import (  # noqa: E402
    AUTHOR,
    FAMILY,
    OPERATOR,
    REVIEWER,
    blocked_decision,
    bundle,
    drill_pass_decision,
)

from tennis_engine.common.clock import SystemClock  # noqa: E402
from tennis_engine.governance.contracts import Principal, Role  # noqa: E402
from tennis_engine.infrastructure.database import build_engine  # noqa: E402
from tennis_engine.models.registry import (  # noqa: E402
    ModelRegistry,
    RegistryRefused,
    build_bundle,
)
from tennis_engine.models.registry_postgres import PostgresRegistryStore  # noqa: E402


def refused(call) -> list[str]:
    try:
        call()
    except RegistryRefused as error:
        return list(error.reasons)
    return []


def main() -> int:
    database_url = os.environ["TEST_DATABASE_URL"]
    os.environ["TENNIS_DATABASE_URL"] = database_url
    config = Config(str(ROOT / "alembic.ini"))
    workdir = Path(tempfile.mkdtemp(prefix="ops02-rollback-"))
    checks: dict[str, bool] = {}
    report: dict[str, object] = {}
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        engine = build_engine(database_url)
        root = workdir / "artifacts"
        first = bundle(root, "v1", seed=1)
        second = bundle(root, "v2", seed=2, rollback_target=first.bundle_id)
        shutil.copytree(root, workdir / "backup")
        registry = ModelRegistry(PostgresRegistryStore(engine), root, SystemClock())
        registry.register(first)
        registry.register(second)
        supported = frozenset({first.feature_set_sha256})

        real = blocked_decision(first.rollback_reference)
        reasons = refused(
            lambda: registry.promote(
                first.bundle_id,
                real,
                actor=REVIEWER,
                reason="OPS-02 drill: real synthetic decision",
                supported_feature_sets=supported,
            )
        )
        report["real_synthetic_decision"] = {"status": real.status.value, "refused": reasons}
        checks["real_decision_refused"] = real.status.value != "PASS" and bool(reasons)
        checks["no_champion_after_refusal"] = registry.champion(FAMILY) is None

        mixed = refused(
            lambda: build_bundle(
                root,
                family=FAMILY,
                version="mixed",
                model_dir=first.model.resolve(root).parent,
                calibrator_dir=second.calibrator.resolve(root).parent,
                evaluation_report=first.evaluation_report.resolve(root),
                rollback_target=None,
                registered_by="ops02-drill",
                registered_at=SystemClock().now(),
            )
        )
        self_review = refused(
            lambda: registry.promote(
                first.bundle_id,
                drill_pass_decision(first.candidate, first.rollback_reference, reviewer=AUTHOR),
                actor=REVIEWER,
                reason="OPS-02 drill: self-approval",
                supported_feature_sets=supported,
            )
        )
        author_switch = refused(
            lambda: registry.promote(
                first.bundle_id,
                drill_pass_decision(first.candidate, first.rollback_reference),
                actor=Principal(identity=AUTHOR, role=Role.POLICY_REVIEWER),
                reason="OPS-02 drill: author switch",
                supported_feature_sets=supported,
            )
        )
        report["refusals"] = {
            "mixed_bundle": mixed,
            "self_approval": self_review,
            "author_switch": author_switch,
        }
        checks["mixed_bundle_refused"] = "CALIBRATOR_BELONGS_TO_ANOTHER_MODEL" in mixed
        checks["self_approval_refused"] = "REVIEWER_IS_AUTHOR" in self_review
        checks["author_switch_refused"] = "AUTHOR_CANNOT_SWITCH" in author_switch

        for target in (first, second):
            registry.promote(
                target.bundle_id,
                drill_pass_decision(target.candidate, target.rollback_reference),
                actor=REVIEWER,
                reason="OPS-02 drill switch with a PASS fixture",
                supported_feature_sets=supported,
            )
        checks["second_is_champion"] = registry.champion(FAMILY) == second

        started = time.perf_counter()
        event = registry.rollback(
            FAMILY, actor=OPERATOR, reason="OPS-02 rollback drill", supported_feature_sets=supported
        )
        loaded = registry.active(FAMILY, supported)
        rollback_seconds = time.perf_counter() - started
        checks["rolled_back_to_declared_target"] = (
            loaded is not None
            and loaded.bundle == first
            and loaded.calibrator is not None
            and loaded.calibrator.base_artifact_sha256 == loaded.artifact.artifact_sha256
        )

        # Make the second bundle champion again, then damage the first bundle's calibrator.
        registry.promote(
            second.bundle_id,
            drill_pass_decision(second.candidate, second.rollback_reference, salt="again"),
            actor=REVIEWER,
            reason="OPS-02 drill switch again",
            supported_feature_sets=supported,
        )
        target = first.calibrator.resolve(root)
        target.chmod(0o644)
        target.write_bytes(target.read_bytes() + b" ")
        damaged = refused(
            lambda: registry.rollback(
                FAMILY,
                actor=OPERATOR,
                reason="OPS-02 damaged target",
                supported_feature_sets=supported,
            )
        )
        checks["damaged_target_refused"] = "ROLLBACK_TARGET:HASH_MISMATCH:calibrator" in damaged
        checks["champion_kept_after_refusal"] = registry.champion(FAMILY) == second
        backup = workdir / "backup" / first.calibrator.path
        target.write_bytes(backup.read_bytes())
        started = time.perf_counter()
        registry.rollback(
            FAMILY,
            actor=OPERATOR,
            reason="OPS-02 restored target",
            supported_feature_sets=supported,
        )
        restored = registry.active(FAMILY, supported)
        restored_seconds = time.perf_counter() - started
        checks["restored_target_rolls_back"] = restored is not None and restored.bundle == first

        events = registry.store.events(FAMILY)
        report.update(
            {
                "bundles": {"v1": str(first.bundle_id), "v2": str(second.bundle_id)},
                "rollback_event": event.model_dump(mode="json"),
                "damaged_rollback_refused": damaged,
                "rollback_seconds": round(rollback_seconds, 3),
                "rollback_after_restore_seconds": round(restored_seconds, 3),
                "events": [f"{item.sequence}:{item.kind.value}" for item in events],
                "checks": checks,
                "status": "PASS" if all(checks.values()) else "FAIL",
            }
        )
        engine.dispose()
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["status"] == "PASS" else 1
    finally:
        command.downgrade(config, "base")
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
