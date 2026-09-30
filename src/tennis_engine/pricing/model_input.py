"""F12 wiring from F09 baseline outputs to the decision `ModelAssessment`.

Conservative mapping choices:

- The selected player's probability comes from canonical player order, never source order.
- The conservative probability is the lower edge of the model's own bootstrap spread for
  that player. It is a model-spread proxy, not a confidence bound on the true probability.
  A prediction without a spread gives no assessment.
- `calibrated` is copied from the prediction. F09 baselines are raw, so the calibration
  gate fails until F11 supplies calibrated output.
- Disagreement is the largest gap between the primary and the other supported baselines.
  Without another supported baseline it is unknown, and the gate fails.
- Baselines are trained on sporting match results, so the semantics are `SPORTING_WIN`.
- Only a SUPPORTED market consensus (more than one bookmaker) confirms a large edge.
"""

from collections.abc import Sequence
from decimal import Decimal
from uuid import UUID

from tennis_engine.common.contracts import VersionRef
from tennis_engine.models.baselines.contracts import (
    BaselinePrediction,
    SupportStatus,
    UncertaintyMethod,
)
from tennis_engine.models.baselines.market import ConsensusOutput

from .decision import ModelAssessment


def _for_selection(
    prediction: BaselinePrediction, selection_player_id: UUID
) -> tuple[Decimal, Decimal | None] | None:
    """Central probability and spread lower edge for the selected player."""
    if prediction.probability_player_one is None:
        return None
    first, second = prediction.player_ids
    spread = prediction.uncertainty
    lower = spread.lower if spread.method != UncertaintyMethod.NONE else None
    upper = spread.upper if spread.method != UncertaintyMethod.NONE else None
    if selection_player_id == first:
        return prediction.probability_player_one, lower
    if selection_player_id == second:
        return Decimal(1) - prediction.probability_player_one, (
            None if upper is None else Decimal(1) - upper
        )
    raise ValueError("The selection is not a player in this prediction")


def assessment_from_baselines(
    primary: BaselinePrediction,
    others: Sequence[BaselinePrediction],
    *,
    match_id: UUID,
    selection_player_id: UUID,
) -> ModelAssessment | None:
    """Return None when the primary baseline cannot give a usable probability."""
    if primary.match_id != match_id or any(item.match_id != match_id for item in others):
        raise ValueError("Every prediction must belong to the decision's match")
    selected = _for_selection(primary, selection_player_id)
    if selected is None:
        return None
    central, low = selected
    if low is None:
        return None
    conservative = min(central, low)
    alternatives = [
        values[0]
        for item in others
        if item.support == SupportStatus.SUPPORTED
        and (values := _for_selection(item, selection_player_id)) is not None
    ]
    disagreement = max((abs(central - value) for value in alternatives), default=None)
    return ModelAssessment(
        model=VersionRef(
            component=primary.model, version=primary.model_version, sha256=primary.artifact_sha256
        ),
        probability=central,
        conservative_probability=conservative,
        semantics="SPORTING_WIN",
        in_supported_domain=primary.support == SupportStatus.SUPPORTED,
        calibrated=primary.calibrated,
        disagreement=disagreement,
        generated_at=primary.predicted_at,
        feature_vector_sha256=primary.snapshot_sha256,
    )


def consensus_for_selection(
    consensus: ConsensusOutput | None, *, match_id: UUID, selection_player_id: UUID
) -> Decimal | None:
    """Consensus probability for the selected player, only when SUPPORTED."""
    if consensus is None or consensus.support != SupportStatus.SUPPORTED:
        return None
    if consensus.match_id != match_id or consensus.probability_player_one is None:
        return None
    first, second = consensus.player_ids
    if selection_player_id == first:
        return consensus.probability_player_one
    if selection_player_id == second:
        return Decimal(1) - consensus.probability_player_one
    return None
