import json
import sys
from pathlib import Path

import pytest

from tennis_engine.governance import cli
from tennis_engine.governance.contracts import (
    PayoutSchedule,
    Principal,
    Purpose,
    ResponsibleUseSchedule,
    Role,
    SourcePolicy,
)


def test_seed_register_has_all_categories_and_no_approvals(store, service):
    root = Path(__file__).resolve().parents[1] / "configs/governance"
    categories = set()
    for path in (root / "sources").glob("*.json"):
        source = SourcePolicy.model_validate_json(path.read_bytes())
        categories.add(source.category)
        assert source.state == "DRAFT" and source.kill_switch
        assert source.reviewer is None and not source.evidence
        store.save(source, expected_revision=0, reason="Initialize draft register")
        assert not service.can_fetch(source.source_id, Purpose.PROTOTYPE).allowed
    assert categories == {"sports", "odds", "regulations", "rankings", "forecasts", "research"}
    bookmakers = set()
    for path in (root / "payouts").glob("*.json"):
        document = PayoutSchedule.model_validate_json(path.read_bytes())
        bookmakers.add(document.bookmaker)
        store.save(document, expected_revision=0, reason="Independent pending review")
        assert document.policies[0].state == "PENDING_REVIEW"
        assert not service.get_payout_policy(
            document.bookmaker, store.clock(), store.clock()
        ).decision.allowed
    assert bookmakers == {"betclic", "superbet", "fortuna"}
    policy = ResponsibleUseSchedule.model_validate_json(
        (root / "responsible_use/internal-shadow.json").read_bytes()
    )
    assert policy.policies[0].disable_recommendations
    assert policy.policies[0].daily.stake == 0


def run_cli(monkeypatch, capsys, tmp_path, *args):
    monkeypatch.setattr(
        sys, "argv", ["tennis-governance", "--database", str(tmp_path / "cli.sqlite3"), *args]
    )
    code = cli.main()
    return code, json.loads(capsys.readouterr().out)


def test_cli_defaults_denied(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        cli, "resolve_principal", lambda _: Principal(identity="reader", role=Role.DASHBOARD)
    )
    code, result = run_cli(monkeypatch, capsys, tmp_path, "init")
    assert code == 0 and result["global_disable"] is True
    code, result = run_cli(monkeypatch, capsys, tmp_path, "check-source", "unknown", "prototype")
    assert code == 2 and result["reason"] == "SOURCE_UNKNOWN"
    code, result = run_cli(
        monkeypatch, capsys, tmp_path, "global-disable", "off", "--reason", "Attempt"
    )
    assert code == 2 and result["error"] == "PermissionError"


def test_cli_append_and_export(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        cli,
        "resolve_principal",
        lambda _: Principal(identity="local-reviewer", role=Role.POLICY_REVIEWER),
    )
    path = Path(__file__).resolve().parents[1] / "configs/governance/sources/betclic-odds.json"
    args = ("apply", "source", str(path), "--expected-revision", "0", "--reason", "Draft source")
    code, result = run_cli(monkeypatch, capsys, tmp_path, *args)
    assert code == 0 and result["revision"] == 1
    code, result = run_cli(monkeypatch, capsys, tmp_path, *args)
    assert code == 2 and result["error"] == "ConflictError"
    code, result = run_cli(monkeypatch, capsys, tmp_path, "export")
    assert code == 0 and len(result["records"]) == 1
    assert result["records"][0]["actor"] == "local-reviewer"


def test_cli_validates_before_mutation_and_redacts_values(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        cli,
        "resolve_principal",
        lambda _: Principal(identity="local-reviewer", role=Role.POLICY_REVIEWER),
    )
    path = tmp_path / "invalid.json"
    path.write_text('{"private-secret-field":"DO_NOT_ECHO"}')
    code, result = run_cli(
        monkeypatch,
        capsys,
        tmp_path,
        "apply",
        "source",
        str(path),
        "--expected-revision",
        "0",
        "--reason",
        "Invalid",
    )
    assert code == 2 and result["error"] == "INVALID_POLICY"
    assert "DO_NOT_ECHO" not in json.dumps(result)


def test_local_account_mapping_defaults_to_viewer(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.getpass, "getuser", lambda: "alice")
    path = tmp_path / "access.json"
    assert cli.resolve_principal(path).role == Role.DASHBOARD
    path.write_text('{"alice":"policy_reviewer"}')
    assert cli.resolve_principal(path).role == Role.POLICY_REVIEWER
    path.write_text('{"alice":"invented-admin"}')
    with pytest.raises(ValueError):
        cli.resolve_principal(path)
