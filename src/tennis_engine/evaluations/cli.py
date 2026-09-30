"""`tennis-agent-eval`: run the offline agent evaluation on frozen fixtures.

The command runs deterministic agents only. It never calls a language model and needs no
API key. Exit codes: 0 PASS, 1 FAIL, 2 BLOCKED or an input error.
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from .agents import AGENTS
from .cases import FixtureError, Split, load_cases
from .harness import Adjudication, Purpose, load_config, run_suite
from .report import write_run

EXIT = {"PASS": 0, "FAIL": 1, "BLOCKED": 2}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="tennis-agent-eval", description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run the evaluation and write the artifacts")
    run.add_argument("--fixtures", type=Path, default=Path("tests/evals/agents"))
    run.add_argument(
        "--release-fixtures",
        type=Path,
        help="Sealed release fixtures, kept outside this repository",
    )
    run.add_argument("--config", type=Path, default=Path("configs/evaluations/agents.json"))
    run.add_argument("--agent", choices=sorted(AGENTS), default="reference")
    run.add_argument(
        "--purpose", choices=["smoke", "integration", "release"], default="integration"
    )
    run.add_argument("--repetitions", type=int, default=1)
    run.add_argument("--reviews", type=Path, help="Completed review-adjudications.jsonl")
    run.add_argument("--out", type=Path, default=Path("var/agent-evals"))
    return root


def splits_for(purpose: Purpose) -> tuple[Split, ...]:
    return ("development",) if purpose == "smoke" else ("development", "validation")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    purpose: Purpose = args.purpose
    if not 1 <= args.repetitions <= 10:
        print("repetitions must be from 1 to 10", file=sys.stderr)
        return 2
    try:
        config = load_config(args.config)
        manifest, cases = load_cases(args.fixtures, splits_for(purpose))
        if args.release_fixtures is not None:
            _, sealed = load_cases(args.release_fixtures, ("release",))
            cases = (*cases, *sealed)
        reviews: list[Adjudication] = []
        if args.reviews is not None:
            adapter = TypeAdapter(Adjudication)
            reviews = [
                adapter.validate_json(line)
                for line in args.reviews.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
    except (OSError, ValueError, ValidationError, FixtureError) as error:
        # Never print file content or paths: report the error type only.
        print(f"input error: {type(error).__name__}", file=sys.stderr)
        return 2
    result = run_suite(
        cases,
        agent=args.agent,
        config=config,
        purpose=purpose,
        repetitions=args.repetitions,
        reviews=reviews,
    )
    directory = write_run(
        args.out,
        result,
        config=config,
        manifest=manifest,
        cases=cases,
        created_at=datetime.now(UTC),
    )
    decision = result.decision
    print(
        json.dumps(
            {
                "run_id": directory.name,
                "decision": decision.decision,
                "failures": list(decision.failures),
                "blocked": list(decision.blocked),
                "cases": len(cases),
                "trajectories": decision.trajectories,
                "critical_trajectories": result.overall.critical_trajectories,
            },
            sort_keys=True,
        )
    )
    return EXIT[decision.decision]


if __name__ == "__main__":
    raise SystemExit(main())
