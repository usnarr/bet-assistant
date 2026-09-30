"""Synthetic model bundles for F11.8/F13.9 registry tests and the rollback drill.

Every artifact is trained on the seeded fictional history of `baseline_support`. The
PASS decision built here is a drill fixture: it lets a test exercise the switch and
rollback paths. It is not evidence about any model, and the real F13 run on this data
stays BLOCKED (see `blocked_decision`).
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from baseline_support import rows, synthetic, training_rows

from tennis_engine.backtesting.contracts import GateStatus
from tennis_engine.backtesting.promotion import (
    LIMITATIONS,
    GateEvidence,
    PromotionGate,
    ReleaseDecision,
)
from tennis_engine.common.ids import stable_id
from tennis_engine.features.contracts import digest
from tennis_engine.features.labels import label_known_at
from tennis_engine.governance.contracts import Principal, Role
from tennis_engine.models.baselines.baseline import predict, train
from tennis_engine.models.baselines.contracts import BaselineKind
from tennis_engine.models.baselines.report import evaluate, model_card
from tennis_engine.models.baselines.storage import write_baseline
from tennis_engine.models.calibration.calibrate import fit_calibrator
from tennis_engine.models.calibration.contracts import CalibrationMethod
from tennis_engine.models.calibration.storage import write_calibrator
from tennis_engine.models.registry import build_bundle

LOCK_SHA = "c" * 64
CODE = "0123456789abcdef"
REGISTERED_AT = datetime(2026, 9, 30, 12, tzinfo=UTC)
AUTHOR = "fixture-model-author"
REVIEWER = Principal(identity="fixture-model-reviewer", role=Role.POLICY_REVIEWER)
OPERATOR = Principal(identity="fixture-operator", role=Role.OPERATOR)
FAMILY = "match-winner"


class World:
    """One synthetic history, trained once per process."""

    _state = None

    @classmethod
    def get(cls):
        if cls._state is None:
            state = synthetic()
            split = state.matches[99][1]
            cls._state = (state, split - timedelta(hours=1))
        return cls._state


def artifacts(root: Path, version: str, seed: int) -> tuple[Path, Path, Path]:
    """Write a baseline, its card, a calibrator and an evaluation report under `root`."""
    state, cutoff = World.get()
    artifact = train(
        training_rows(state, cutoff),
        BaselineKind.RANKING,
        training_cutoff=cutoff,
        version=version,
        bootstrap_draws=20,
        seed=seed,
    )
    final = state.matches[-1][1] + timedelta(days=1)
    held_out = [(s, label) for s, label in rows(state, until=final) if s.as_of > cutoff]
    predictions = [
        (predict(snapshot, artifact, predicted_at=snapshot.as_of), label)
        for snapshot, label in held_out
    ]
    metrics, unsupported = evaluate([(p, label, {"tour": "atp"}) for p, label in predictions])
    card = model_card(
        artifact,
        evaluation=metrics,
        unsupported_rows=unsupported,
        data_availability="PROSPECTIVE",
        code_revision=CODE,
        dependency_lock_sha256=LOCK_SHA,
        rollback_target=None,
        license_notes="Synthetic fixture data only",
    )
    model_dir = write_baseline(root, artifact, card)
    supported = [(p, label) for p, label in predictions if p.probability_player_one is not None]
    times = sorted(p.as_of for p, _ in supported)
    calibrator = fit_calibrator(
        [(p, label_known_at(state.h.store, p.match_id, final)) for p, _ in supported],
        window_start=cutoff + timedelta(minutes=30),
        validation_start=times[len(times) * 2 // 3],
        window_end=final,
        version=f"{version}-platt",
        min_fit_rows=10,
        min_validation_rows=5,
        methods=(CalibrationMethod.PLATT_SYMMETRIC,),
    )
    calibrator_dir = write_calibrator(root, calibrator, code_revision=CODE)
    report = root / "evaluations" / f"{FAMILY}-{version}.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps([item.model_dump(mode="json") for item in metrics], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return model_dir, calibrator_dir, report


def bundle(root: Path, version: str, seed: int, rollback_target: UUID | None = None):
    model_dir, calibrator_dir, report = artifacts(root, version, seed)
    return build_bundle(
        root,
        family=FAMILY,
        version=version,
        model_dir=model_dir,
        calibrator_dir=calibrator_dir,
        evaluation_report=report,
        rollback_target=rollback_target,
        registered_by=AUTHOR,
        registered_at=REGISTERED_AT,
    )


def drill_pass_decision(
    candidate: str,
    rollback_target: str,
    *,
    author: str = AUTHOR,
    reviewer: str | None = REVIEWER.identity,
    salt: str = "",
) -> ReleaseDecision:
    """A PASS decision fixture for switch and rollback drills. Not model evidence."""
    gates = tuple(
        GateEvidence(gate=gate, status=GateStatus.PASS, detail="OPS-02 drill fixture")
        for gate in PromotionGate
    )
    body = {"candidate": candidate, "rollback": rollback_target, "reviewer": reviewer, "s": salt}
    content = digest(body)
    return ReleaseDecision(
        decision_id=stable_id("release-decision", content),
        run_id=UUID(int=1),
        run_sha256="d" * 64,
        candidate=candidate,
        baseline="baseline-global-elo",
        config_version="drill-fixture",
        config_sha256="e" * 64,
        status=GateStatus.PASS,
        gates=gates,
        comparison=None,
        author=author,
        reviewer=reviewer,
        reviewed_at=REGISTERED_AT if reviewer else None,
        rollback_target=rollback_target,
        evaluated_at=REGISTERED_AT,
        decided_at=REGISTERED_AT,
        limitations=(*LIMITATIONS, "OPS-02 drill fixture; not evidence about a model"),
        content_sha256=content,
    )


def blocked_decision(rollback_target: str = "no-champion") -> ReleaseDecision:
    """The real F13 promotion decision on the synthetic walk-forward run, with the
    committed release configuration and every optional input present. It is BLOCKED,
    because the release thresholds are not frozen yet."""
    from backtest_support import START, boundaries, world
    from test_backtesting import ROOT, candidates

    from tennis_engine.backtesting.contracts import load_config
    from tennis_engine.backtesting.promotion import Review, decide_promotion
    from tennis_engine.backtesting.runner import run_walk_forward
    from tennis_engine.backtesting.splits import walk_forward
    from tennis_engine.features.contracts import AvailabilityMode
    from tennis_engine.features.core import CORE_SET
    from tennis_engine.features.dataset import build_dataset

    state = world(count=150)
    manifest, snapshots = build_dataset(
        state.h.store,
        CORE_SET,
        state.rows(("24h", "1h")),
        name="registry-synthetic",
        mode=AvailabilityMode.PROSPECTIVE,
        cutoff_rule="first known start minus 24 hours and 1 hour",
        created_at=START,
        code_revision="0000000",
        source_versions={},
    )
    split = walk_forward(snapshots, boundaries(first_day=35, days=12), name="registry-synthetic")
    run = run_walk_forward(
        state.h.store,
        snapshots,
        split,
        candidates(state),
        name="registry-synthetic",
        dataset_id=manifest.dataset_id,
        tagger=state.tagger,
    )
    return decide_promotion(
        run,
        candidate="baseline-ranking",
        config=load_config(ROOT / "configs" / "evaluations" / "release.json"),
        author=AUTHOR,
        evaluated_at=START,
        decided_at=START,
        leakage_passed=True,
        rerun_sha256=run.content_sha256,
        rollback_target=rollback_target,
        review=Review(reviewer=REVIEWER.identity, reviewed_at=START, approved=True),
    )


D = Decimal
