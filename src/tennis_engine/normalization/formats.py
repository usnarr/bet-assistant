"""Point-in-time lookup of the deciding-set rule for a canonical match (F04.5, F07).

The rule comes from the match's edition, draw stage and best-of format. Only versions
observed by ``as_of`` count. A missing or ``UNKNOWN`` rule returns ``None``; the caller
must abstain and must not assume a default format.
"""

from datetime import datetime
from uuid import UUID

from .contracts import BestOf, DecidingSetRule, DrawStage, EditionFormatVersion
from .store import IdentityStore


def deciding_set_known_at(
    store: IdentityStore, match_id: UUID, as_of: datetime
) -> EditionFormatVersion | None:
    match = store.match(match_id)
    if match.draw_stage == DrawStage.UNKNOWN or match.best_of == BestOf.UNKNOWN:
        return None
    known = [
        item
        for item in store.edition_formats(match.edition_id, match.draw_stage, match.best_of)
        if item.availability.observed_at <= as_of
    ]
    if not known or known[-1].deciding_set == DecidingSetRule.UNKNOWN:
        return None
    return known[-1]
