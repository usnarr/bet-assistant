"""F18.5 and F18.7: frozen fixtures, independent scoring and the release decision."""

import importlib.util
import json
from collections import Counter
from pathlib import Path

import pytest

from tennis_engine.agents.contracts import AgentRole
from tennis_engine.evaluations import cli
from tennis_engine.evaluations.cases import FixtureError, check_cases, load_cases
from tennis_engine.evaluations.harness import Adjudication, load_config, run_suite

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "evals" / "agents"
CONFIG = ROOT / "configs" / "evaluations" / "agents.json"


def generator():
    spec = importlib.util.spec_from_file_location(
        "build_agent_fixtures", ROOT / "scripts" / "build_agent_fixtures.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cases():
    return load_cases(FIXTURES, ("development", "validation"))[1]


@pytest.fixture(scope="module")
def config():
    return load_config(CONFIG)


def run(cases, config, agent="reference", **kwargs):
    options = {"purpose": "integration", "repetitions": 1} | kwargs
    return run_suite(cases, agent=agent, config=config, **options)


def test_committed_fixtures_equal_the_generator_output():
    for relative, content in generator().build().items():
        on_disk = (FIXTURES / relative).read_bytes().replace(b"\r\n", b"\n")
        assert on_disk == content, relative


def test_fixture_coverage(cases):
    development = [item for item in cases if item.split == "development"]
    assert len(development) >= 36
    for role in AgentRole:
        groups = {item.group for item in development if item.role == role and item.group != "cross"}
        assert groups == {"valid", "incomplete", "adversarial"}, role
    families = {item.cross_family for item in cases if item.group == "cross"}
    assert families == {"injection", "temporal", "authorization", "retries", "resource", "handoff"}
    assert Counter(item.split for item in cases) == {"development": 36, "validation": 9}
    # Expected outcomes are independent: no fixture holds an agent answer.
    for item in cases:
        assert "final" not in item.model_dump_json()


def test_tampered_or_mixed_fixtures_block(tmp_path, cases):
    for path in FIXTURES.rglob("*.json"):
        target = tmp_path / path.relative_to(FIXTURES)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    victim = tmp_path / "development" / "AG-VR-STALE-001.json"
    victim.write_text(victim.read_text().replace("STALE_QUOTE", "QUOTE_FRESH"))
    with pytest.raises(FixtureError, match="hash mismatch"):
        load_cases(tmp_path, ("development",))
    moved = cases[0].model_copy(update={"split": "validation", "case_id": "AG-DI-VALID-999"})
    with pytest.raises(FixtureError, match="two splits"):
        check_cases((cases[0], moved))


def test_reference_agent_has_no_critical_violation(cases, config):
    result = run(cases, config, repetitions=3)
    assert result.overall.trajectories == 3 * len(cases)
    assert result.overall.critical_trajectories == 0
    assert result.decision.failures == ()
    # Proposed gates and pending human review block a release decision.
    assert result.decision.decision == "BLOCKED"
    assert set(result.decision.blocked) == {"GATES_NOT_FROZEN", "HUMAN_REVIEW_PENDING"}
    assert all(item.passed for item in result.scores)
    # Deterministic agents give the same scores on each repetition.
    by_rep = {
        rep: [
            s.model_dump(exclude={"repetition", "trace_id"})
            for s in result.scores
            if s.repetition == rep
        ]
        for rep in (1, 2, 3)
    }
    assert by_rep[1] == by_rep[2] == by_rep[3]


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        ("injection-follower", "ALL:CRITICAL_VIOLATIONS"),
        ("fabricator", "ALL:CRITICAL_VIOLATIONS"),
        ("override", "AG-VR:CRITICAL_VIOLATIONS"),
        ("over-refuser", "ALL:UNNECESSARY_REFUSAL"),
    ],
)
def test_scorers_catch_each_failure_class(cases, config, agent, expected):
    result = run(cases, config, agent=agent)
    assert result.decision.decision == "FAIL"
    assert expected in result.decision.failures
    if agent == "injection-follower":
        violations = {v for s in result.scores for v in s.critical_violations}
        assert "FORBIDDEN_ACTION:place_bet" in violations
        assert "ORACLE:FORBIDDEN_ACTION:place_bet" in violations
        # The gateway denied it: no output with a BET was used.
        assert all(s.protected for s in result.scores if s.critical_violations)
    if agent == "override":
        violations = {v for s in result.scores for v in s.critical_violations}
        assert "VERIFIER:HARD_GATE_OVERRIDE" in violations


def test_missing_budget_caps_block(cases, config):
    stripped = config.model_copy(update={"models": {}})
    result = run(cases[:3], stripped)
    assert result.decision.decision == "BLOCKED"
    assert "BUDGET_CAPS_MISSING" in result.decision.blocked
    assert result.scores == ()


def test_release_needs_a_sealed_set_and_repetitions(cases, config):
    result = run(cases, config, purpose="release")
    assert {"SEALED_RELEASE_SET_MISSING", "REPETITIONS_BELOW_RELEASE"} <= set(
        result.decision.blocked
    )
    smoke = run(cases[:5], config, purpose="smoke")
    assert "SMOKE_SUBSET_TOO_SMALL" in smoke.decision.blocked
    assert "ROLE_NOT_COVERED:AG-MO" in smoke.decision.blocked


def test_frozen_gates_and_complete_reviews_can_pass(cases, config):
    frozen = config.model_copy(update={"status": "FROZEN"})
    first = run(cases, frozen)
    queue = first.adjudications
    assert queue and {item.reason for item in queue} >= {"SAMPLE"}
    reviews = [
        item.model_copy(update={"reviewer": "synthetic-reviewer", "verdict": "AGREE"})
        for item in queue
    ]
    passed = run(cases, frozen, reviews=reviews)
    assert passed.decision.decision == "PASS"
    reviews[0] = reviews[0].model_copy(update={"verdict": "DISAGREE"})
    failed = run(cases, frozen, reviews=reviews)
    assert failed.decision.decision == "FAIL"
    assert any(item.startswith("HUMAN_DISAGREES") for item in failed.decision.failures)
    # The stratified sample has at least one item per role and group.
    strata = {(s.prefix, s.group) for s in first.scores}
    by_trace = {s.trace_id: (s.prefix, s.group) for s in first.scores}
    assert {by_trace[item.trace_id] for item in queue} == strata


def test_cli_writes_artifacts_without_local_paths(tmp_path, capsys):
    code = cli.main(
        [
            "run",
            "--fixtures",
            str(FIXTURES),
            "--config",
            str(CONFIG),
            "--out",
            str(tmp_path),
            "--repetitions",
            "1",
        ]
    )
    assert code == 2  # BLOCKED: gates not frozen and reviews pending
    summary = json.loads(capsys.readouterr().out)
    directory = tmp_path / summary["run_id"]
    names = {path.name for path in directory.iterdir()}
    assert names == {
        "manifest.json",
        "traces.jsonl",
        "case-scores.jsonl",
        "metrics.json",
        "review-adjudications.jsonl",
        "report.md",
        "release-decision.json",
    }
    manifest = json.loads((directory / "manifest.json").read_text())
    import hashlib

    for name, digest in manifest["files"].items():
        assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest
    home = str(Path.home())
    for path in directory.iterdir():
        text = path.read_text(encoding="utf-8")
        assert home not in text and str(tmp_path) not in text and "Users" not in text
    assert summary["critical_trajectories"] == 0


def test_cli_input_errors_do_not_echo_content(tmp_path, capsys):
    bad = tmp_path / "agents.json"
    bad.write_text('{"secret": "synthetic-secret-canary"}')
    assert cli.main(["run", "--config", str(bad), "--out", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "synthetic-secret-canary" not in err and str(tmp_path) not in err
    assert cli.main(["run", "--repetitions", "0", "--out", str(tmp_path)]) == 2


def test_adjudication_contract():
    item = Adjudication(case_id="AG-VR-STALE-001", repetition=1, trace_id="t", reason="SAMPLE")
    assert item.verdict == "PENDING" and item.reviewer is None
