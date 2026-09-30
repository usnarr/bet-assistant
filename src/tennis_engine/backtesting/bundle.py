"""Immutable evaluation bundle ``<root>/<run_id>/`` (evaluation plan, run reports).

Predictions are JSON Lines, not Parquet: the project has no Parquet dependency yet.
``manifest.json`` lists the SHA-256 of every other file and is written last. An existing
run directory is never overwritten. Store only relative names; never write local paths.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from tennis_engine.features.contracts import canonical_json

from .contracts import EvaluationConfig
from .metrics import ForecastMetrics
from .promotion import ReleaseDecision
from .runner import RunResult

FILES = (
    "predictions.jsonl",
    "fits.jsonl",
    "metrics.json",
    "failures.jsonl",
    "report.md",
    "release-decision.json",
)


def _lines(rows: Sequence[bytes]) -> bytes:
    return b"".join(row + b"\n" for row in rows)


def render_report(
    run: RunResult,
    metrics: Sequence[ForecastMetrics],
    decision: ReleaseDecision | None,
    *,
    limitations: Sequence[str],
) -> str:
    lines = [
        f"# Evaluation run {run.name}",
        "",
        f"Run `{run.run_id}`, content `{run.content_sha256}`, split `{run.split_sha256}`.",
        "",
        "## Forecast metrics (log loss primary; accuracy and AUC secondary)",
        "",
        "| Model | Segment | Rows | Scorable | Coverage | Log loss | Brier | Slope | Status |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for item in metrics:
        lines.append(
            f"| {item.model} | {item.segment} | {item.rows} | {item.scorable} | {item.coverage} "
            f"| {item.log_loss} | {item.brier} | {item.calibration_slope} | {item.status} |"
        )
    if decision is not None:
        lines += ["", f"## Release decision: {decision.status}", ""]
        lines += [f"- `{gate.gate}`: {gate.status} ({gate.detail})" for gate in decision.gates]
    lines += ["", "## Limitations", ""] + [f"- {item}" for item in limitations]
    return "\n".join(lines) + "\n"


def write_bundle(
    root: Path,
    run: RunResult,
    *,
    config: EvaluationConfig,
    metrics: Sequence[ForecastMetrics],
    decision: ReleaseDecision | None,
    provenance: Mapping[str, str],
    failures: Sequence[Mapping[str, object]] = (),
    limitations: Sequence[str] = (),
) -> Path:
    """``provenance`` holds code revision, lock hash, dataset and model hashes."""
    target = root / str(run.run_id)
    target.mkdir(parents=True, exist_ok=False)
    contents = {
        "predictions.jsonl": _lines(
            [canonical_json(item.model_dump(mode="json")) for item in run.predictions]
        ),
        "fits.jsonl": _lines([canonical_json(item.model_dump(mode="json")) for item in run.fits]),
        "metrics.json": canonical_json([item.model_dump(mode="json") for item in metrics]),
        "failures.jsonl": _lines([canonical_json(dict(item)) for item in failures]),
        "report.md": render_report(run, metrics, decision, limitations=limitations).encode(),
        "release-decision.json": canonical_json(
            decision.model_dump(mode="json") if decision else {"status": "NOT_EVALUATED"}
        ),
    }
    hashes = {}
    for name in FILES:
        (target / name).write_bytes(contents[name])
        hashes[name] = hashlib.sha256(contents[name]).hexdigest()
    manifest = {
        "run_id": str(run.run_id),
        "run_sha256": run.content_sha256,
        "split_id": str(run.split_id),
        "split_sha256": run.split_sha256,
        "dataset_id": str(run.dataset_id) if run.dataset_id else None,
        "models": list(run.models),
        "config": config.model_dump(mode="json"),
        "config_sha256": config.sha256,
        "provenance": dict(provenance),
        "files": hashes,
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target
