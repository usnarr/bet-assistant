"""Write the evaluation artifacts of one run.

Files: `manifest.json`, `traces.jsonl`, `case-scores.jsonl`, `metrics.json`,
`review-adjudications.jsonl`, `report.md` and `release-decision.json`. The files hold no
local path, no evidence text and no secret. `manifest.json` holds the SHA-256 of each
other file.
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from tennis_engine.agents.roles import ROLES
from tennis_engine.agents.tools import TOOL_SCHEMA_VERSION

from .cases import EvalCase, Manifest, split_hashes
from .harness import EvalConfig, GroupMetrics, Rate, SuiteResult

FILES = (
    "traces.jsonl",
    "case-scores.jsonl",
    "metrics.json",
    "review-adjudications.jsonl",
    "report.md",
    "release-decision.json",
)


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def _lines(items: list[Any]) -> str:
    return "".join(json.dumps(item, sort_keys=True) + "\n" for item in items)


def _rate(value: Rate) -> str:
    if value.rate is None:
        return "n/a (0)"
    return f"{value.numerator}/{value.denominator} ({value.lower95}-{value.upper95})"


def _row(name: str, group: GroupMetrics) -> str:
    return (
        f"| {name} | {group.cases} | {group.trajectories} | {group.critical_trajectories} | "
        f"{_rate(group.numeric_fidelity)} | {_rate(group.evidence_fidelity)} | "
        f"{_rate(group.abstention_recall)} | {_rate(group.benign_completion)} | "
        f"{_rate(group.unnecessary_refusal)} | {_rate(group.budget_compliance)} |"
    )


def markdown(result: SuiteResult, config: EvalConfig, fixture_set: str) -> str:
    decision = result.decision
    lines = [
        "# Agent evaluation report",
        "",
        f"- Agent: `{decision.agent}` (deterministic, no language model).",
        f"- Purpose: `{decision.purpose}`. Repetitions: {decision.repetitions}.",
        f"- Fixture set: `{fixture_set}`. Gates: `{config.version}` ({config.status}).",
        f"- Decision: **{decision.decision}**.",
        f"- Failures: {', '.join(decision.failures) or 'none'}.",
        f"- Blocked: {', '.join(decision.blocked) or 'none'}.",
        "",
        "Rates show numerator/denominator and the Wilson 95% interval. Small denominators",
        "give wide intervals. These results come from synthetic fixtures only.",
        "",
        "| Group | Cases | Runs | Critical | Numeric | Evidence | Abstention | Benign | "
        "Refusal | Budget |",
        "|---|---:|---:|---:|---|---|---|---|---|---|",
        _row("ALL", result.overall),
    ]
    lines += [_row(name, group) for name, group in result.by_role.items()]
    lines += [_row(f"group:{name}", group) for name, group in result.by_group.items()]
    pending = sum(1 for item in result.adjudications if item.verdict == "PENDING")
    lines += [
        "",
        f"Human review queue: {len(result.adjudications)} items, {pending} pending.",
        f"Harness errors: {len(result.errors)}.",
        "",
    ]
    return "\n".join(lines)


def write_run(
    root: Path,
    result: SuiteResult,
    *,
    config: EvalConfig,
    manifest: Manifest,
    cases: tuple[EvalCase, ...],
    created_at: datetime,
) -> Path:
    hashes = split_hashes(cases)
    identity = {
        "agent": result.decision.agent,
        "config": config.digest(),
        "splits": hashes,
        "repetitions": result.decision.repetitions,
        "purpose": result.decision.purpose,
    }
    run_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
    directory = root / run_id
    directory.mkdir(parents=True, exist_ok=True)
    contents = {
        "traces.jsonl": _lines([item.model_dump(mode="json") for item in result.traces]),
        "case-scores.jsonl": _lines([item.model_dump(mode="json") for item in result.scores]),
        "metrics.json": _json(
            {
                "overall": result.overall.model_dump(mode="json"),
                "by_role": {k: v.model_dump(mode="json") for k, v in result.by_role.items()},
                "by_family": {k: v.model_dump(mode="json") for k, v in result.by_family.items()},
                "by_group": {k: v.model_dump(mode="json") for k, v in result.by_group.items()},
                "errors": list(result.errors),
            }
        ),
        "review-adjudications.jsonl": _lines(
            [item.model_dump(mode="json") for item in result.adjudications]
        ),
        "report.md": markdown(result, config, manifest.fixture_set),
        "release-decision.json": _json(result.decision.model_dump(mode="json")),
    }
    for name, body in contents.items():
        (directory / name).write_text(body, encoding="utf-8", newline="\n")
    summary = {
        "schema_version": "1.0",
        "run_id": run_id,
        "created_at": created_at.isoformat(),
        "agent": result.decision.agent,
        "model": "fake/" + result.decision.agent + "-agent",
        "roles": {
            spec.role.prefix: {"version": spec.version, "prompt_sha256": spec.prompt_sha256}
            for spec in ROLES.values()
        },
        "tool_schema_version": TOOL_SCHEMA_VERSION,
        "oracle_versions": sorted({item.oracle_version for item in cases}),
        "config_version": config.version,
        "config_sha256": config.digest(),
        "fixture_set": manifest.fixture_set,
        "split_hashes": hashes,
        "cases": len(cases),
        "repetitions": result.decision.repetitions,
        "files": {
            name: hashlib.sha256(contents[name].encode("utf-8")).hexdigest() for name in FILES
        },
    }
    (directory / "manifest.json").write_text(_json(summary), encoding="utf-8", newline="\n")
    return directory
