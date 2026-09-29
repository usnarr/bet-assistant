"""F05.4, F05.6, F05.7: quote intervals, heartbeats and one versioned actionability rule.

Observations are append-only. Intervals are derived from them and change only when the
quote fingerprint changes; an unchanged price seen again is a heartbeat. Actionability
uses only observations available at the decision time, so a later `valid_to` or a later
poll can never make an earlier decision look fresh. Failed polls are not observations and
cannot extend a quote's lifetime.
"""

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated

from pydantic import Field

from tennis_engine.common.clock import require_aware
from tennis_engine.common.contracts import Contract, Identifier, Timestamp

from .contracts import EventState, QuoteState, RawQuote


class QuoteObservation(Contract):
    quote: RawQuote
    observed_at: Timestamp
    parser_version: Identifier
    raw_content_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    scheduled_start: Timestamp | None
    event_state: EventState


class QuoteInterval(Contract):
    key: tuple[str, str, str, str]
    quote: RawQuote
    valid_from: Timestamp  # First observation with this fingerprint.
    valid_to: Timestamp | None  # First observation of the next fingerprint.
    observed_last_at: Timestamp
    heartbeats: int  # Repeated observations after the first one.


def _ordered(observations: Iterable[QuoteObservation]) -> list[QuoteObservation]:
    return sorted(observations, key=lambda item: (item.observed_at, item.raw_content_sha256))


def build_intervals(observations: Iterable[QuoteObservation]) -> tuple[QuoteInterval, ...]:
    """Summarize observations of any keys into intervals, ordered by key then time."""
    by_key: dict[tuple[str, str, str, str], list[QuoteObservation]] = {}
    for item in observations:
        by_key.setdefault(item.quote.key, []).append(item)
    intervals: list[QuoteInterval] = []
    for key in sorted(by_key):
        current: QuoteInterval | None = None
        for item in _ordered(by_key[key]):
            if current is not None and current.quote.fingerprint() == item.quote.fingerprint():
                current = current.model_copy(
                    update={
                        "observed_last_at": item.observed_at,
                        "heartbeats": current.heartbeats + 1,
                    }
                )
                continue
            if current is not None:
                intervals.append(current.model_copy(update={"valid_to": item.observed_at}))
            current = QuoteInterval(
                key=key,
                quote=item.quote,
                valid_from=item.observed_at,
                valid_to=None,
                observed_last_at=item.observed_at,
                heartbeats=0,
            )
        if current is not None:
            intervals.append(current)
    return tuple(intervals)


class ActionabilityPolicy(Contract):
    """Proposed initial settings (F05.7); suspend publication when quotas cannot meet them."""

    version: Identifier = "quote-actionability-proposed-v1"
    min_consecutive_observations: Annotated[int, Field(ge=2, strict=True)] = 2
    min_spacing_seconds: Annotated[int, Field(ge=0, strict=True)] = 20
    max_age_seconds: Annotated[int, Field(gt=0, strict=True)] = 60


class ActionabilityReason(StrEnum):
    NO_OBSERVATION = "NO_OBSERVATION"
    QUOTE_SUSPENDED = "QUOTE_SUSPENDED"
    QUOTE_CLOSED = "QUOTE_CLOSED"
    QUOTE_STALE = "QUOTE_STALE"
    QUOTE_WITHDRAWN = "QUOTE_WITHDRAWN"
    QUOTE_UNCONFIRMED = "QUOTE_UNCONFIRMED"
    START_UNKNOWN = "START_UNKNOWN"
    EVENT_STARTED = "EVENT_STARTED"
    EVENT_CANCELLED = "EVENT_CANCELLED"
    EVENT_STATE_UNKNOWN = "EVENT_STATE_UNKNOWN"


class Actionability(Contract):
    actionable: bool
    reasons: tuple[ActionabilityReason, ...]
    policy_version: Identifier
    evaluated_at: Timestamp
    observation: QuoteObservation | None
    confirmations: int
    expires_at: Timestamp | None


def evaluate_actionability(
    observations: Sequence[QuoteObservation],
    *,
    at: datetime,
    policy: ActionabilityPolicy,
    event_polls: Sequence[datetime] = (),
) -> Actionability:
    """Decide if one quote key is actionable at `at`.

    `event_polls` are the successful polls that listed the event. A later successful poll
    without this selection means the quote was withdrawn.
    """
    at = require_aware(at)
    keys = {item.quote.key for item in observations}
    if len(keys) > 1:
        raise ValueError("Actionability is evaluated for one quote key at a time")
    visible = [item for item in _ordered(observations) if item.observed_at <= at]
    if not visible:
        return Actionability(
            actionable=False,
            reasons=(ActionabilityReason.NO_OBSERVATION,),
            policy_version=policy.version,
            evaluated_at=at,
            observation=None,
            confirmations=0,
            expires_at=None,
        )
    last = visible[-1]
    reasons: list[ActionabilityReason] = []
    if last.quote.state == QuoteState.SUSPENDED:
        reasons.append(ActionabilityReason.QUOTE_SUSPENDED)
    elif last.quote.state == QuoteState.CLOSED:
        reasons.append(ActionabilityReason.QUOTE_CLOSED)
    # Expiry is exclusive: a quote exactly `max_age_seconds` old is already stale.
    if at - last.observed_at >= timedelta(seconds=policy.max_age_seconds):
        reasons.append(ActionabilityReason.QUOTE_STALE)
    if any(last.observed_at < require_aware(poll) <= at for poll in event_polls):
        reasons.append(ActionabilityReason.QUOTE_WITHDRAWN)
    start = last.scheduled_start
    if last.event_state == EventState.STARTED:
        reasons.append(ActionabilityReason.EVENT_STARTED)
    elif last.event_state == EventState.CANCELLED:
        reasons.append(ActionabilityReason.EVENT_CANCELLED)
    elif last.event_state == EventState.UNKNOWN:
        reasons.append(ActionabilityReason.EVENT_STATE_UNKNOWN)
    if start is None:
        reasons.append(ActionabilityReason.START_UNKNOWN)
    elif at >= start and ActionabilityReason.EVENT_STARTED not in reasons:
        reasons.append(ActionabilityReason.EVENT_STARTED)

    # Trailing run with the same price and the same start: a start change needs new
    # confirmation even when the price did not move.
    run = []
    for item in reversed(visible):
        if (
            item.quote.fingerprint() != last.quote.fingerprint()
            or item.scheduled_start != start
            or item.event_state != last.event_state
        ):
            break
        run.append(item)
    spaced = [
        item
        for item in run
        if last.observed_at - item.observed_at >= timedelta(seconds=policy.min_spacing_seconds)
    ]
    confirmed = len(run) >= policy.min_consecutive_observations and bool(spaced)
    if not confirmed:
        reasons.append(ActionabilityReason.QUOTE_UNCONFIRMED)
    expires_at = last.observed_at + timedelta(seconds=policy.max_age_seconds)
    if start is not None:
        expires_at = min(expires_at, start)
    return Actionability(
        actionable=not reasons,
        reasons=tuple(reasons),
        policy_version=policy.version,
        evaluated_at=at,
        observation=last,
        confirmations=len(run),
        expires_at=None if reasons else expires_at,
    )
