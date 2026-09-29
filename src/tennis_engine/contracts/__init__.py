"""Versioned domain contracts consumed by feature modules."""

from .domain import (
    AuditLineage,
    Availability,
    AvailabilityClass,
    CanonicalMatch,
    FeatureVector,
    GateResult,
    PayoutResult,
    ProbabilityOutput,
    QuoteObservation,
    RawIngestionRecord,
    Recommendation,
)

__all__ = [
    "AuditLineage",
    "Availability",
    "AvailabilityClass",
    "CanonicalMatch",
    "FeatureVector",
    "GateResult",
    "PayoutResult",
    "ProbabilityOutput",
    "QuoteObservation",
    "RawIngestionRecord",
    "Recommendation",
]
