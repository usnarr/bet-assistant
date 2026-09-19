"""Generate a local F01 evidence bundle using synthetic approvals only."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from tennis_engine.governance.contracts import (
    PayoutSchedule,
    Principal,
    Purpose,
    ResponsibleUseSchedule,
    Role,
    SourcePolicy,
)
from tennis_engine.governance.retention import RetentionService
from tennis_engine.governance.service import GovernanceService, PermissionDenied
from tennis_engine.governance.store import GovernanceStore, digest


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    output = root / "var/f01-demo" / str(uuid4())
    output.mkdir(parents=True)
    now = datetime(2026, 9, 19, 10, tzinfo=UTC)
    principal = Principal(identity="synthetic-demo-reviewer", role=Role.POLICY_REVIEWER)
    store = GovernanceStore(output / "governance.sqlite3", principal, lambda: now)
    try:
        for directory, model in [
            ("sources", SourcePolicy),
            ("payouts", PayoutSchedule),
            ("responsible_use", ResponsibleUseSchedule),
        ]:
            for path in sorted((root / "configs/governance" / directory).glob("*.json")):
                store.save(
                    model.model_validate_json(path.read_bytes()),
                    expected_revision=0,
                    reason="Disabled draft from the F01 register",
                )

        common = {
            "version": "synthetic-v1",
            "owner": "synthetic-demo-owner",
            "reviewer": principal.identity,
            "reviewed_at": now,
            "review_due_at": now + timedelta(days=365),
            "effective_from": now,
            "effective_until": now + timedelta(days=365),
        }

        def evidence(scope: str, document_id: str) -> list[dict[str, str]]:
            content = f"Self-authored synthetic fixture for {scope}; not real policy.".encode()
            _, sha256 = store.archive_document(
                document_id,
                scope,
                "synthetic://F01-demo",
                content,
                reason="Synthetic demonstration evidence",
            )
            (output / f"{document_id}.txt").write_bytes(content)
            return [{"document_id": document_id, "sha256": sha256}]

        source = SourcePolicy.model_validate(
            common
            | {
                "source_id": "synthetic-sports",
                "category": "sports",
                "access_method": "file",
                "terms_reference": "synthetic://F01-demo",
                "commercial_use": "no",
                "state": "PROTOTYPE_APPROVED",
                "allowed_purposes": ["prototype"],
                "kill_switch": False,
                "quota": {"requests": 1, "window_seconds": 1, "concurrency": 1},
                "retention": {"raw_days": 1, "basis": "Synthetic example"},
                "evidence": evidence("source:synthetic-sports", "synthetic-terms"),
            }
        )
        source_revision = store.save(source, expected_revision=0, reason="Synthetic prototype only")
        store.save(
            PayoutSchedule.model_validate(
                {
                    "bookmaker": "synthetic-book",
                    "policies": [
                        common
                        | {
                            "bookmaker": "synthetic-book",
                            "state": "APPROVED",
                            "payout_rule_version": "synthetic-rule",
                            "settlement_rule_version": "synthetic-rule",
                            "evidence": evidence("payout:synthetic-book", "synthetic-rules"),
                        }
                    ],
                }
            ),
            expected_revision=0,
            reason="Synthetic policy references only; no money calculated",
        )
        store.save(
            ResponsibleUseSchedule.model_validate(
                {
                    "account_scope": "synthetic-account",
                    "policies": [
                        common
                        | {
                            "account_scope": "synthetic-account",
                            "ledger_scope": "virtual",
                            "state": "APPROVED",
                            "daily": {"stake": "1.00", "count": 1},
                            "weekly": {"stake": "1.00", "count": 1},
                            "monthly": {"stake": "1.00", "count": 1},
                            "max_event_exposure": "1.00",
                            "max_open_exposure": "1.00",
                            "max_bankroll_fraction": "0.01",
                            "drawdown_stop": "0.01",
                            "disable_recommendations": False,
                            "evidence": evidence(
                                "responsible_use:synthetic-account", "synthetic-limits"
                            ),
                        }
                    ],
                }
            ),
            expected_revision=0,
            reason="Synthetic virtual limits only",
        )
        store.set_global_disable(False, reason="Synthetic demonstration only")
        service = GovernanceService(store)
        retention = RetentionService(store)
        retention.archive_raw(
            "synthetic-sample", source.source_id, Purpose.PROTOTYPE, b"Synthetic sports sample"
        )

        def gate():
            return service.publication_gate(
                [source.source_id], "synthetic-book", "synthetic-account", Purpose.PROTOTYPE
            ).model_dump(mode="json")

        observations = {"before_revocation": gate()}
        calls = []
        now += timedelta(seconds=1)
        revoked = SourcePolicy.model_validate(
            source.model_dump()
            | {
                "version": "synthetic-v2",
                "state": "SUSPENDED",
                "kill_switch": True,
            }
        )
        store.save(
            revoked, expected_revision=source_revision, reason="Revoke after a fetch was queued"
        )
        try:
            service.execute_fetch(
                source.source_id, Purpose.PROTOTYPE, lambda: calls.append("fetched")
            )
        except PermissionDenied as error:
            observations["queued_fetch_after_revocation"] = error.decision.model_dump(mode="json")
        observations["publication_after_revocation"] = gate()
        now += timedelta(days=1)
        observations["retention"] = {
            "deleted": retention.expire_due(),
            "replay_available": retention.replay_available("synthetic-sample"),
        }
        passed = (
            observations["before_revocation"]["allowed"]
            and not calls
            and not observations["queued_fetch_after_revocation"]["allowed"]
            and not observations["publication_after_revocation"]["allowed"]
            and observations["retention"] == {"deleted": 1, "replay_available": False}
        )
        report = {
            "evaluation": "SYS-01",
            "engineering_demo": "PASS" if passed else "FAIL",
            "external_approval_gate": "BLOCKED",
            "synthetic": True,
            "lock_sha256": digest((root / "uv.lock").read_bytes()),
            "observations": observations,
        }
        (output / "register-export.json").write_text(json.dumps(store.export(), indent=2) + "\n")
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "output": str(output),
                    "engineering_demo": report["engineering_demo"],
                    "external_approval_gate": "BLOCKED",
                },
                indent=2,
            )
        )
        if not passed:
            raise SystemExit(1)
    finally:
        store.close()


if __name__ == "__main__":
    main()
