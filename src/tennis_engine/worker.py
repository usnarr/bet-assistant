"""Worker process skeleton gated on infrastructure readiness."""

import logging
import time

from tennis_engine.common.logging import configure_logging
from tennis_engine.infrastructure.health import InfrastructureProbe, public_checks, ready
from tennis_engine.infrastructure.settings import Settings


def main() -> int:
    settings = Settings()
    configure_logging(getattr(logging, settings.log_level))
    logger = logging.getLogger("tennis_engine.worker")
    probe = InfrastructureProbe(settings)
    while True:
        checks = probe.check()
        if ready(checks):
            logger.info("worker ready", extra={"context": public_checks(checks)})
            break
        logger.warning("worker waiting for dependencies", extra={"context": public_checks(checks)})
        time.sleep(settings.worker_poll_seconds)
    while True:
        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())
