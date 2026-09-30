"""Market consensus as a walk-forward candidate, from F05 quote history at each cutoff.

The candidate has no fitted parameters. At each snapshot cutoff it reads the quotes that
F05 history had observed by then, maps them with the event mapping known then, and runs
the F09 consensus. Nothing after the cutoff is read, and closing prices are never used.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from tennis_engine.features.contracts import FeatureSnapshot, digest
from tennis_engine.ingestion.bookmakers.contracts import CanonicalQuote
from tennis_engine.ingestion.bookmakers.history import QuoteHistory, QuoteKey
from tennis_engine.models.baselines.baseline import TrainingRow
from tennis_engine.models.baselines.market import ConsensusConfig, consensus

from .replay import canonical_quote
from .runner import Fitted, Output

QuoteSource = Callable[[UUID, datetime], Sequence[CanonicalQuote]]


def history_quotes(
    history: QuoteHistory, keys: Callable[[UUID], Sequence[QuoteKey]]
) -> QuoteSource:
    """Quotes known at ``as_of`` for the quote keys of one match.

    Each key contributes its latest observation by ``as_of``, mapped with the latest event
    mapping known by ``as_of``. A key without a mapping then contributes nothing.
    """

    def source(match_id: UUID, as_of: datetime) -> Sequence[CanonicalQuote]:
        quotes = []
        for key in keys(match_id):
            observations = history.store.observations(key, as_of)
            mappings = history.store.mappings(key[0], key[1], as_of)
            if not observations or not mappings:
                continue
            quote = canonical_quote(observations[-1], mappings[-1])
            if quote is not None and quote.match_id == match_id:
                quotes.append(quote)
        return quotes

    return source


@dataclass(frozen=True)
class _FittedConsensus:
    quotes: QuoteSource
    config: ConsensusConfig

    @property
    def version(self) -> str:
        return self.config.version

    @property
    def artifact_sha256(self) -> str:
        return digest(self.config.model_dump(mode="json"))

    def predict(self, snapshot: FeatureSnapshot) -> Output:
        result = consensus(
            self.quotes(snapshot.match_id, snapshot.as_of),
            match_id=snapshot.match_id,
            player_ids=snapshot.player_ids,
            as_of=snapshot.as_of,
            config=self.config,
        )
        return Output(result.support, result.reasons, result.probability_player_one)


@dataclass(frozen=True)
class ConsensusCandidate:
    """F09 market consensus. The fit ignores ``rows``; the method has no parameters."""

    quotes: QuoteSource
    config: ConsensusConfig = field(default_factory=ConsensusConfig)
    name: str = "market-consensus"

    def fit(self, rows: Sequence[TrainingRow], training_cutoff: datetime) -> Fitted:
        return _FittedConsensus(self.quotes, self.config)
