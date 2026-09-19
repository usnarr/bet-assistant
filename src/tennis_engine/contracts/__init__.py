"""Versioned domain contracts consumed by feature modules."""

from .domain import (
    AuditLineage,
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
    "CanonicalMatch",
    "FeatureVector",
    "GateResult",
    "PayoutResult",
    "ProbabilityOutput",
    "QuoteObservation",
    "RawIngestionRecord",
    "Recommendation",
]
