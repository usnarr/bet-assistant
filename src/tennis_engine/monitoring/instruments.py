"""F15.3 metric names for the API and the F14 read path.

Labels hold only route templates, decision values, reason codes and configured source IDs.
They never hold tokens, raw paths with IDs, payloads or personal data.
"""

from collections.abc import Callable, Iterable
from datetime import datetime

from tennis_engine.contracts.domain import RecommendationStatus
from tennis_engine.serving.contracts import ResponsibleUseStatus, SourceHealth

from .metrics import Family, MetricsRegistry, Sample

SOURCE_STATUS = Family(
    "tennis_source_status",
    "gauge",
    "1 for the current F14 source-health status of each source.",
    ("source_id", "status"),
)
SOURCE_AGE = Family(
    "tennis_source_observation_age_seconds",
    "gauge",
    "Age of the latest quote observation. Absent when no observation exists.",
    ("source_id",),
)
RECOMMENDATIONS_ALLOWED = Family(
    "tennis_recommendations_allowed",
    "gauge",
    "1 when the responsible-use policy and the global stop allow recommendations.",
    ("account_scope",),
)


def reason_code(reason: str) -> str:
    """`SOURCE:<id>:SOURCE_DISABLED` becomes `SOURCE:SOURCE_DISABLED`. IDs are dropped."""
    parts = reason.split(":")
    return parts[0] if len(parts) == 1 else f"{parts[0]}:{parts[-1]}"


class ServingMetrics:
    def __init__(self, registry: MetricsRegistry) -> None:
        self.registry = registry
        self.requests = registry.counter(
            "tennis_http_requests_total",
            "HTTP requests by route template, method and status.",
            ("route", "method", "status"),
        )
        self.latency = registry.histogram(
            "tennis_http_request_duration_seconds",
            "HTTP request duration by route template.",
            ("route",),
        )
        self.rechecked = registry.counter(
            "tennis_rechecked_records_total",
            "Current F14 records rechecked at read time, by recorded and served decision.",
            ("recorded", "served"),
        )
        self.read_blocks = registry.counter(
            "tennis_read_time_blocks_total",
            "Read-time reasons that made a current record not actionable.",
            ("reason",),
        )
        self.dependency_errors = registry.counter(
            "tennis_dependency_errors_total",
            "Store failures that gave HTTP 503, by exception type.",
            ("error",),
        )

    def record_view(
        self,
        recorded: RecommendationStatus,
        served: RecommendationStatus,
        reasons: Iterable[str],
    ) -> None:
        self.rechecked.inc(recorded=recorded.value, served=served.value)
        for reason in reasons:
            self.read_blocks.inc(reason=reason_code(reason))


def register_serving_collector(
    registry: MetricsRegistry,
    health: Callable[[datetime], Iterable[SourceHealth]],
    responsible: Callable[[datetime], ResponsibleUseStatus],
    now: Callable[[], datetime],
) -> None:
    """Source health and responsible-use state, computed on each scrape."""

    def collect() -> Iterable[Sample]:
        at = now()
        samples = []
        for row in health(at):
            source = (("source_id", row.source_id),)
            samples.append(Sample(SOURCE_STATUS.name, 1, source + (("status", row.status),)))
            if row.age_seconds is not None:
                samples.append(Sample(SOURCE_AGE.name, float(row.age_seconds), source))
        state = responsible(at)
        samples.append(
            Sample(
                RECOMMENDATIONS_ALLOWED.name,
                1 if state.allowed else 0,
                (("account_scope", state.account_scope),),
            )
        )
        return samples

    registry.register_collector(
        "serving", collect, (SOURCE_STATUS, SOURCE_AGE, RECOMMENDATIONS_ALLOWED)
    )
